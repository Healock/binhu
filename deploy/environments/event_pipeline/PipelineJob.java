import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.StatementSet;
import org.apache.flink.table.api.TableEnvironment;

/** Run private SQL without the SQL client's command echo (which includes secrets). */
public final class PipelineJob {
    private static final String UID_DEV_REVISIONS = "dev-revisions";
    private static final String UID_DEV_METADATA = "dev-metadata";
    /**
     * Table SQL is translated into streaming transformations by the planner.
     * Enabling ALWAYS makes those transformations carry explicit UIDs.  A
     * single StatementSet keeps both sinks behind one Kafka source and one
     * consumer group, while the fixed format keeps UIDs stable across
     * recompiles.  It never includes the run id or a secret.
     */
    private static String uid(String prefix) {
        return "dev-pipeline-<id>_<transformation>";
    }

    private static void uid(TableEnvironment table, String prefix) {
        table.getConfig().getConfiguration().setString("table.exec.uid.generation", "ALWAYS");
        table.getConfig().getConfiguration().setString("table.exec.uid.format", uid(prefix));
    }

    private static String safeFailureCode(Throwable failure) {
        Throwable cause = failure;
        for (int depth = 0; depth < 12 && cause != null; depth++, cause = cause.getCause()) {
            String message = cause.getMessage();
            String lower = message == null ? "" : message.toLowerCase(Locale.ROOT);
            if (lower.contains("distinct aggregates with different arguments")) {
                return "flink_multiple_distinct_aggregate_keys";
            }
            if (lower.contains("no match found for function signature")) {
                return "flink_sql_function_signature_unmatched";
            }
            if (lower.contains("cannot apply")) {
                return "flink_sql_type_mismatch";
            }
            if (lower.contains("does not exist") || lower.contains("not found")) {
                return "flink_sql_identifier_missing";
            }
            if (lower.contains("not supported")) {
                return "flink_sql_feature_unsupported";
            }
        }
        return failure.getClass().getSimpleName().equals("ValidationException")
            ? "flink_sql_validation_error_unclassified"
            : "flink_statement_error_unclassified";
    }

    public static void main(String[] args) throws Exception {
        String environment = System.getenv("APP_ENVIRONMENT");
        if (args.length != 0 || !("development".equals(environment) || "staging".equals(environment))) {
            throw new IllegalArgumentException("Non-Production job identity required");
        }
        String sql = Files.readString(Path.of("/opt/flink/private/pipeline.sql"));
        TableEnvironment table = TableEnvironment.create(EnvironmentSettings.inStreamingMode());
        StatementSet statementSet = table.createStatementSet();
        int index = 0;
        try {
            for (String statement : sql.split(";")) {
                index++;
                String command = statement.strip();
                if (command.startsWith("SET ")) {
                    String[] pair = command.substring(4).split(" = ", 2);
                    if (pair.length != 2) throw new IllegalArgumentException();
                    table.getConfig().getConfiguration().setString(
                        pair[0].substring(1, pair[0].length() - 1),
                        pair[1].substring(1, pair[1].length() - 1));
                } else if (!command.isBlank()) {
                    if (command.startsWith("INSERT INTO dev_revisions") ||
                        command.startsWith("INSERT INTO dev_task_metadata")) {
                        uid(table, command.startsWith("INSERT INTO dev_revisions")
                            ? UID_DEV_REVISIONS : UID_DEV_METADATA);
                        statementSet.addInsertSql(command);
                    } else {
                        String ddl = command;
                        table.executeSql(ddl);
                    }
                }
            }
            statementSet.execute();
        } catch (Exception failure) {
            // SQL parser/planner exceptions may contain a JDBC credential.
            StringBuilder types = new StringBuilder();
            Throwable cause = failure;
            for (int depth = 0; depth < 12 && cause != null; depth++, cause = cause.getCause()) {
                types.append(cause.getClass().getSimpleName()).append(" ");
            }
            throw new IllegalStateException("Non-Production statement " + index + " failed: "
                + safeFailureCode(failure) + " " + types);
        }
    }
}
