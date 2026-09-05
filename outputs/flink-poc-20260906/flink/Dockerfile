ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG CDC_VERSION
ARG CDC_SHA256

# The target build host must provide a verified base-image digest and the
# Maven artifact checksum. The downloaded JAR is never accepted unchecked.
ADD https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-mysql-cdc/${CDC_VERSION}/flink-sql-connector-mysql-cdc-${CDC_VERSION}.jar /tmp/mysql-cdc.jar
RUN test -n "${CDC_SHA256}" \
    && echo "${CDC_SHA256}  /tmp/mysql-cdc.jar" | sha256sum -c - \
    && install -m 0644 /tmp/mysql-cdc.jar /opt/flink/lib/flink-sql-connector-mysql-cdc-${CDC_VERSION}.jar \
    && rm -f /tmp/mysql-cdc.jar
# The build needs target-host network access; the resulting runtime has no
# published ports and only joins the isolated internal POC network.
