package binhu.shadow.flink;

import org.apache.flink.core.execution.CheckpointingMode;
import org.apache.flink.api.common.functions.MapFunction;
import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.state.MapState;
import org.apache.flink.api.common.state.MapStateDescriptor;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.api.common.serialization.SimpleStringSchema;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.KeyedProcessFunction;
import org.apache.flink.streaming.api.functions.sink.PrintSinkFunction;
import org.apache.flink.util.Collector;

import java.util.Locale;
import java.util.Set;
import java.util.HashSet;
import java.time.Instant;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.JsonNode;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.core.JsonParser;
import org.apache.flink.shaded.jackson2.com.fasterxml.jackson.databind.DeserializationFeature;
import java.util.regex.Pattern;

/**
 * Metadata-only Kafka checkpoint protocol smoke job.
 *
 * <p>This is deliberately not a business projection. It consumes the one
 * run-scoped protocol topic, extracts an allow-listed envelope, keys state by
 * task_id + source_id, and prints a bounded metadata summary. The input JSON is never
 * forwarded to the sink.</p>
 */
public final class TaskEventCheckpointJob {
    private static final String EXPECTED_BOOTSTRAP_SERVERS =
            "kafka-1:9092,kafka-2:9092,kafka-3:9092";
    private static final String EXPECTED_TOPIC =
            "binhu.shadow.flink.kshadow-20260906t084957z-fcbad2";
    private static final String EXPECTED_RUN_ID =
            "KSHADOW-20260906T084957Z-fcbad2";
    private static final String CONSUMER_GROUP =
            "binhu-flink-checkpoint-kshadow-20260906";
    private static final long CHECKPOINT_INTERVAL_MS = 10_000L;
    private static final int MAX_EVENT_BYTES = 16 * 1024;

    private static final Pattern UUID = Pattern.compile(
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}");
    private static final Pattern RUN_ID = Pattern.compile(
            "KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}");
    private static final Pattern TASK_ID = Pattern.compile(
            "t_(?:fullchain|rental_check|police_stats|suspect_unrevoked|"
                    + "suspect_return|delivery_industry|group_rental|"
                    + "suzhou_police|traffic_police):[1-9][0-9]*");
    private static final Pattern EVENT_TYPE = Pattern.compile(
            "task\\.(?:saved|claimed|assigned|reviewed|archived|created|deleted)");

    private TaskEventCheckpointJob() {
    }

    public static void main(String[] args) throws Exception {
        String bootstrapServers = requireFixedEnvironment(
                "KAFKA_BOOTSTRAP_SERVERS", EXPECTED_BOOTSTRAP_SERVERS);
        String topic = requireFixedEnvironment("KAFKA_PROTOCOL_TOPIC", EXPECTED_TOPIC);
        String runId = requireFixedEnvironment("KAFKA_RUN_ID", EXPECTED_RUN_ID);

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(1);
        env.enableCheckpointing(CHECKPOINT_INTERVAL_MS, CheckpointingMode.EXACTLY_ONCE);
        env.getCheckpointConfig().setCheckpointTimeout(60_000L);
        env.getCheckpointConfig().setMinPauseBetweenCheckpoints(1_000L);
        env.getCheckpointConfig().setMaxConcurrentCheckpoints(1);

        KafkaSource<String> source = KafkaSource.<String>builder()
                .setBootstrapServers(bootstrapServers)
                .setTopics(topic)
                .setGroupId(CONSUMER_GROUP)
                .setStartingOffsets(OffsetsInitializer.earliest())
                .setValueOnlyDeserializer(new SimpleStringSchema())
                .build();

        DataStream<EventMetadata> metadata = env
                .fromSource(source, org.apache.flink.api.common.eventtime.WatermarkStrategy.noWatermarks(),
                        "shadow protocol Kafka source").uid("kafka-source-v1")
                .map(new MetadataOnlyParser(runId))
                .name("parse metadata-only task envelope").uid("metadata-parser-v1");

        metadata
                .keyBy(event -> event.taskId + "|" + event.sourceId)
                .process(new EventCountProcessFunction())
                .name("task revision fence and recovery counter").uid("revision-fence-v1")
                .addSink(new PrintSinkFunction<>())
                .name("metadata-only smoke output").uid("metadata-output-v1");

        env.execute("Binhu shadow Kafka checkpoint protocol smoke");
    }

    private static String requireFixedEnvironment(String name, String expected) {
        String value = System.getenv(name);
        if (!expected.equals(value)) {
            throw new IllegalStateException(name + " must be the fixed isolated shadow value");
        }
        return value;
    }

    private static final Set<String> FIELDS = Set.of("schema_version", "event_id", "event_type",
            "task_id", "source_id", "revision", "operation_id", "changed_fields", "timestamp", "environment", "run_id");
    private static final Set<String> CHANGED = Set.of("address", "community", "inspector", "check_result",
            "task_state", "small_community", "person_tags", "task_graph", "daily_report");

    public static EventMetadata parse(String json, String expectedRunId) {
        try {
            if (json == null || json.getBytes(java.nio.charset.StandardCharsets.UTF_8).length > MAX_EVENT_BYTES)
                throw new IllegalArgumentException();
            ObjectMapper mapper = new ObjectMapper();
            mapper.enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
            mapper.enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
            JsonNode node = mapper.readTree(json);
            if (!node.isObject()) throw new IllegalArgumentException();
            Set<String> names = new HashSet<>(); node.fieldNames().forEachRemaining(names::add);
            if (!names.equals(FIELDS)) throw new IllegalArgumentException();
            if (integer(node,"schema_version",1) != 1) throw new IllegalArgumentException();
            String eventId = text(node,"event_id",UUID);
            String eventType = text(node,"event_type",EVENT_TYPE);
            String taskId = text(node,"task_id",TASK_ID);
            long localId = Long.parseLong(taskId.substring(taskId.indexOf(':')+1));
            if (localId <= 0) throw new IllegalArgumentException();
            long sourceId = integer(node,"source_id",1);
            long revision = integer(node,"revision",0);
            text(node,"operation_id",UUID);
            String timestamp = text(node,"timestamp",Pattern.compile(
                    "[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?Z"));
            Instant.parse(timestamp);
            if (!"shadow".equals(text(node,"environment",Pattern.compile("shadow"))) ||
                !expectedRunId.equals(text(node,"run_id",RUN_ID))) throw new IllegalArgumentException();
            JsonNode changes = node.get("changed_fields");
            if (!changes.isArray() || changes.size() > 64) throw new IllegalArgumentException();
            Set<String> unique = new HashSet<>();
            for (JsonNode c : changes) {
                if (!c.isTextual() || !CHANGED.contains(c.textValue()) || !unique.add(c.textValue()))
                    throw new IllegalArgumentException();
            }
            return new EventMetadata(eventId,eventType,taskId,sourceId,revision,expectedRunId);
        } catch (Exception ignored) {
            // Never attach the parser exception: its message can include input body.
            throw new IllegalArgumentException("invalid shadow metadata envelope");
        }
    }

    private static String text(JsonNode node,String key,Pattern pattern) {
        JsonNode v=node.get(key);
        if (v==null || !v.isTextual() || !pattern.matcher(v.textValue()).matches())
            throw new IllegalArgumentException();
        return v.textValue();
    }
    private static long integer(JsonNode node,String key,long minimum) {
        JsonNode v=node.get(key);
        if (v==null || !v.isIntegralNumber() || !v.canConvertToLong() || v.longValue()<minimum)
            throw new IllegalArgumentException();
        return v.longValue();
    }
    private static final class MetadataOnlyParser implements MapFunction<String, EventMetadata> {
        private static final long serialVersionUID = 1L;
        private final String expectedRunId;
        private MetadataOnlyParser(String runId) { expectedRunId=runId; }
        public EventMetadata map(String json) { return parse(json,expectedRunId); }
    }

    private static final class EventCountProcessFunction
            extends KeyedProcessFunction<String, EventMetadata, String> {
        private static final long serialVersionUID = 1L;
        private transient ValueState<Long> countState;
        private transient ValueState<Long> revisionState;
        private transient MapState<String, Boolean> eventIds;

        @Override
        public void open(org.apache.flink.configuration.Configuration parameters) throws Exception {
            ValueStateDescriptor<Long> descriptor =
                    new ValueStateDescriptor<>("event_id-count", Types.LONG);
            countState = getRuntimeContext().getState(descriptor);
            revisionState = getRuntimeContext().getState(new ValueStateDescriptor<>("highest-revision", Types.LONG));
            eventIds = getRuntimeContext().getMapState(
                    new MapStateDescriptor<>("processed-event-ids", Types.STRING, Types.BOOLEAN));
        }

        @Override
        public void processElement(
                EventMetadata event,
                Context context,
                Collector<String> out) throws Exception {
            Long previous = countState.value();
            Long old = revisionState.value();
            boolean repeated = eventIds.contains(event.eventId);
            String status = repeated ? "DUPLICATE" : old == null || event.revision > old ? "APPLIED" : event.revision == old ? "DUPLICATE" : "STALE";
            long count = previous == null ? 1L : previous + (repeated ? 0L : 1L);
            if (!repeated) {
                eventIds.put(event.eventId, Boolean.TRUE);
            }
            countState.update(count);
            if (status.equals("APPLIED")) revisionState.update(event.revision);
            // Only fixed envelope metadata is emitted; the input JSON/body is
            // intentionally absent from this output record.
            out.collect(String.format(
                    Locale.ROOT,
                    "run_id=%s event_id=%s event_type=%s task_id=%s source_id=%d revision=%d count=%d status=%s highest=%d",
                    event.runId, event.eventId, event.eventType, event.taskId,
                    event.sourceId, event.revision, count, status, revisionState.value()));
        }
    }

    /** POJO containing only fields allowed in the protocol smoke output. */
    public static final class EventMetadata {
        public String eventId;
        public String eventType;
        public String taskId;
        public long sourceId;
        public long revision;
        public String runId;

        public EventMetadata() {
        }

        private EventMetadata(
                String eventId,
                String eventType,
                String taskId,
                long sourceId,
                long revision,
                String runId) {
            this.eventId = eventId;
            this.eventType = eventType;
            this.taskId = taskId;
            this.sourceId = sourceId;
            this.revision = revision;
            this.runId = runId;
        }
    }

}
