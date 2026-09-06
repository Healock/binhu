# Flink Kafka checkpoint protocol smoke

This is a metadata protocol job for the isolated project
`binhu-kafka-shadow-20260906`, run `KSHADOW-20260906T084957Z-fcbad2`.
It consumes `binhu.shadow.flink.kshadow-20260906t084957z-fcbad2`, using
`task_id + source_id` as keyed state. Its counter and highest revision survive
checkpoint restoration. Equal revisions emit `DUPLICATE`, lower revisions
emit `STALE`. This is revision-level protocol classification, not a complete
business event_id deduplication or business result comparison implementation.

The parser uses Flink shaded Jackson, rejects duplicate/extra JSON keys,
invalid numeric types and out-of-range int64 values, and emits only fixed
metadata. `MetadataContractCheck.java` exercises 16 accepted/rejected cases
inside the exact runtime image. Invalid metadata stops this smoke job with a
fixed error; production quarantine/retry processing is still required.

## Reproducible runtime inputs

`dependencies.lock.json` contains the actual Flink image digest, Kafka
connector SHA-256, and Eclipse ECJ compiler SHA-256. Download these outside
the internal runtime network and verify their digests. Compilation runs
offline in the verified Flink image (which contains a JRE, not javac/jar):
ECJ compiles against the explicit image `/opt/flink/lib/*.jar` classpath;
Python zipfile packages class files with a Main-Class manifest. Run
`python build.py` from the approved server root after copying this directory
under `flink/`. Builds produce a new candidate; do not replace a JAR while
its inode is mounted in running containers.

The Compose file is an overlay of the root Kafka and derived Compose files.
Copy it to `docker-compose.flink.yml`; jar and connector paths resolve from
that isolated root. `FLINK_IMAGE` must contain the locked digest. No host
ports or additional networks are created. JobManager uses 768 MiB process
memory; TaskManager uses 1536 MiB and one slot.

Initialize only the project checkpoint volume using the `flink-init-checkpoints`
profile service (explicit root entrypoint, UID/GID 9999, mode 750), then start
JobManager and TaskManager. The normal Flink entrypoint drops privileges even
when Docker is passed `-u 0`, so it cannot initialize a root-owned fresh volume.

Submission must include the fixed bootstrap, topic and run environment.
Inspect `/overview` from the internal network for a registered TaskManager
before submitting. Checkpoint interval is 10 seconds. Fixed restart delay is
30 seconds, three attempts; the earlier 5-second delay exhausted attempts
before the TaskManager finished registering.

## Verified / still pending

Real shadow evidence: `artifacts/flink-recovery-02-before.json`,
`flink-recovery-02-after.json`, `flink-post-recovery-output.log`.
TaskManager restart recovered state and completed a new checkpoint; revision
3/5/6 then yielded STALE/DUPLICATE/APPLIED with count continuing from 3 to 6.

This job does not call MySQL, Redis, external services, or Backend readback.
Address matching, person labels, task graph, daily statistics, HTTP revision
fencing, cross-system output, dual comparison and 75-user testing remain
separate acceptance stages. Print output is diagnostic and is not a
transactional/exactly-once sink. Keep the Python worker and MySQL truth.
