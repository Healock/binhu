# syntax=docker/dockerfile:1.7
# Offline packaging only: all inputs must already be present in the build
# context. No dependency download is permitted by this file.
ARG FLINK_IMAGE_DIGEST
FROM flink:1.20.1-scala_2.12-java17@${FLINK_IMAGE_DIGEST}

ARG FLINK_IMAGE_DIGEST
ARG KAFKA_CONNECTOR_SHA256
RUN --network=none test -n "$FLINK_IMAGE_DIGEST" \
    && printf '%s' "$FLINK_IMAGE_DIGEST" | grep -Eq '^sha256:[0-9a-f]{64}$' \
    && test -n "$KAFKA_CONNECTOR_SHA256" \
    && printf '%s' "$KAFKA_CONNECTOR_SHA256" | grep -Eq '^[0-9a-f]{64}$'

COPY dependencies/flink-sql-connector-kafka-3.3.0-1.20.jar \
     /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar
COPY target/flink-kafka-checkpoint-smoke-1.0.0-shadow.jar \
     /opt/flink/usrlib/flink-kafka-checkpoint-smoke.jar

RUN --network=none printf '%s  %s\n' "$KAFKA_CONNECTOR_SHA256" \
      /opt/flink/lib/flink-sql-connector-kafka-3.3.0-1.20.jar | sha256sum -c -
