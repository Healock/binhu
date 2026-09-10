import java.nio.file.Files;
import java.nio.file.Path;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.TableEnvironment;

/** Run private SQL without the SQL client's command echo (which includes secrets). */
public final class PipelineJob {
    public static void main(String[] args) throws Exception {
        if (args.length != 0 || !"development".equals(System.getenv("APP_ENVIRONMENT"))) {
            throw new IllegalArgumentException("Dev job identity required");
        }
        String sql = Files.readString(Path.of("/opt/flink/private/pipeline.sql"));
        TableEnvironment table = TableEnvironment.create(EnvironmentSettings.inStreamingMode());
        try {
            for (String statement : sql.split(";")) {
                String command = statement.strip();
                if (command.startsWith("SET ")) {
                    String[] pair = command.substring(4).split(" = ", 2);
                    if (pair.length != 2) throw new IllegalArgumentException();
                    table.getConfig().getConfiguration().setString(
                        pair[0].substring(1, pair[0].length() - 1),
                        pair[1].substring(1, pair[1].length() - 1));
                } else if (!command.isBlank()) {
                    table.executeSql(command);
                }
            }
        } catch (Exception ignored) {
            // SQL parser/planner exceptions may contain a JDBC credential.
            throw new IllegalStateException("Dev job submission failed; private configuration requires review");
        }
    }
}
