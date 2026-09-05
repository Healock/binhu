# Kafka KRaft shadow skeleton

This compose file is an isolated protocol and failure-behavior harness. It is
not a production capacity estimate. Verify both image digests and generate a
unique cluster id on the target shadow host before use. Do not use `latest`,
production credentials, production networks, or production topics.

The three brokers use separate named volumes and an internal-only network.
Keep the host with no published ports. Validate broker stop/restart, replica
availability, duplicate delivery, ordering, retry/DLQ and replay before any
Flink job is introduced. Events must remain metadata-only and consumers must
read derived inputs through `/internal/v1/derived-input`; they must not open
their own MySQL connection.

When event volume exceeds roughly 100,000 events/day, stop using this harness
as a sizing proxy and create a separate partition/replica/disk assessment.
