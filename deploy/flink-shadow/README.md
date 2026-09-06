# Flink Kafka checkpoint protocol smoke

This directory is an isolated protocol smoke skeleton for run
`KSHADOW-20260906T084957Z-fcbad2` in Compose project
`binhu-kafka-shadow-20260906`. It consumes only
`binhu.shadow.protocol.kshadow-20260906t084957z-fcbad2` from the existing
shadow Kafka network at `kafka-1:9092,kafka-2:9092,kafka-3:9092`.

The Java 17 job uses `KafkaSource<String>`, keys records by `event_id`, and
stores one `ValueState<Long>` counter per event. Checkpointing is configured
for 10 seconds and `EXACTLY_ONCE`. The sink emits a fixed allow-list of event
metadata (`run_id`, `event_id`, `event_type`, `task_id`, `source_id`,
`revision`, and the counter). It never emits the input JSON, task正文, address,
ID number, phone number, or arbitrary body content.

This is a protocol checkpoint smoke job. It is not a business projection,
dual-track comparison, capacity result, production enablement, or evidence of
cross-system exactly-once behavior. It does not connect to MySQL, Redis,
Tencent, the residence-permit service, or any external network. The existing
internal Kafka network is referenced as an external network; this Compose file
does not create a new global network and publishes no host ports.

## Locked offline inputs

The only direct Kafka runtime dependency is the SQL connector fat JAR
`org.apache.flink:flink-sql-connector-kafka:3.3.0-1.20`, recorded in
`dependencies.lock.json`. The host must verify that this JAR contains
`org.apache.flink.connector.kafka.source.KafkaSource`; if the downloaded
artifact does not, stop and update the lock with the matching
`flink-connector-kafka` artifact before compiling. Do not silently substitute
an unpinned version.

Fill the `digest` and `sha256` fields in `dependencies.lock.json` from the
verified host artifacts. The Compose variable `FLINK_IMAGE_DIGEST` must be the
full value `sha256:<64 lowercase hex characters>`. The connector build
argument `KAFKA_CONNECTOR_SHA256` must be the 64-character lowercase digest.
Place the connector JAR at the locked path under `dependencies/`.

Compile with Java 17 and Maven offline after the dependency cache has already
been populated by the deployment operator:

```text
mvn -o -DskipTests package
```

The command must be run from this directory. No Dockerfile step downloads a
dependency: its network is disabled and it checks both digest formats before
copying the prebuilt job and connector JAR. The Dockerfile is an optional
immutable runtime packaging path; the Compose smoke path mounts the verified
local artifacts read-only.

Before starting, set `KAFKA_NETWORK` to the already existing internal network
from the Kafka shadow project and set `KAFKA_RUN_ID` exactly to
`KSHADOW-20260906T084957Z-fcbad2`. Start the JobManager and TaskManager, then
submit the one-shot client:

```text
docker compose --env-file .env -f docker-compose.yml up -d flink-jobmanager flink-taskmanager
docker compose --env-file .env -f docker-compose.yml --profile submit run --rm flink-submit
```

The actual container startup, Kafka consumption, checkpoint creation and
recovery must be accepted in the target shadow environment. This workstation
does not provide Docker, Flink, Kafka, or real MySQL, so no runtime acceptance
is claimed here.
