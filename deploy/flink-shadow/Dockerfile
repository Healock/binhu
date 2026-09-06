# Optional immutable packaging after build.py has verified/compiled inputs.
ARG FLINK_IMAGE
FROM ${FLINK_IMAGE}
ARG KAFKA_CONNECTOR_SHA256
ARG JOB_SHA256
COPY dependencies/flink-sql-connector-kafka-3.3.0-1.20.jar /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar
COPY target/flink-kafka-checkpoint-smoke-1.0.0-shadow.jar /opt/flink/usrlib/flink-kafka-checkpoint-smoke.jar
RUN printf '%s  %s\n' "$KAFKA_CONNECTOR_SHA256" /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar | sha256sum -c - \
    && printf '%s  %s\n' "$JOB_SHA256" /opt/flink/usrlib/flink-kafka-checkpoint-smoke.jar | sha256sum -c -
