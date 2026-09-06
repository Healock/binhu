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

## Mirror preparation

The host already has `https://docker.1panel.live` configured as a Docker
registry mirror. `docker manifest inspect` uses its own registry connection;
it does not establish whether the daemon mirror used by `docker pull` works.
Check `/v2/`, the exact image manifest, and blob download independently.
Use explicit `docker.1panel.live/<repository>@sha256:<verified digest>` image
references for this project so both manifest inspection and pulls take the
same route. Keep HTTPS verification enabled and never send platform credentials
to the mirror. Hash verification checks content integrity relative to the
mirror manifest; it is not an upstream publisher signature verification.

Store manifest responses, headers, image locks, pull logs and RepoDigests under
the isolated deployment directory's `artifacts/`. Download fixed release tags
only to resolve a digest; run Compose with digests. Do not restart Docker or
edit the host daemon configuration for this project. Slow downloads must have
a finite timeout and must not be reported as completed before image inspection
succeeds.

Run `python3 prepare_images.py --output-dir artifacts --timeout 25` on the
shadow host, in an existing artifact directory without previous lock outputs.
The helper verifies the index header/body digest and the selected amd64 child
descriptor/header/body digest. It writes a lock and image-only env candidate;
it does not pull images or overwrite the deployment `.env`. It uses an explicit
application User-Agent (the mirror currently rejects Python's default agent),
bounded manifest sizes and timeouts, and rejects cross-host redirects.

Each broker is limited to 1 CPU / 1 GiB (512 MiB JVM heap), and Registry to
0.5 CPU / 768 MiB. Apache Kafka's image uses `CLUSTER_ID` to format storage.
Kafka volumes are Compose-project-scoped, with no fixed global volume names.
The image's implicit `/etc/kafka/secrets` and `/mnt/shared/config` volumes are
covered with bounded tmpfs mounts so container creation leaves no anonymous
volumes. Verify actual mounts after starting or recreating the brokers.
Use replication factor 2 and `min.insync.replicas=2` with producer `acks=all`:
losing a replica deliberately pauses affected writes until ISR recovers.

The current Apicurio `mem` image is temporary protocol storage. Registry
restart loses registrations; export/re-register schema definitions before use.
This is not a durable Registry deployment or evidence of failure recovery.
The repository currently provides infrastructure only: the Kafka Relay and
Flink business projections still require implementation and acceptance.

The protocol skeleton uses PLAINTEXT on the project-only internal network.
No host ports are published. Service authentication/ACLs must be implemented
and exercised before this is considered an authenticated business event bus.
