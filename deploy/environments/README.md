# Binhu environment provisioning

This directory provisions the isolated `development` and `staging` projects.
It never reuses production volumes and never accepts a production database as a
runtime target. Run `python provision.py preview --environment <development|staging>`
before `apply`; `apply` requires an explicit server maintenance window.

Dev is intended for Kafka/Redis/Flink integration; Staging is intended for a
sanitized production copy and load testing. These roles are acceptance targets,
not evidence that either data pipeline has been verified.

`development_shadow_migrate.py` captures hashes and sizes for an explicit set
of top-level configuration files. It copies no configuration values, secrets,
artifacts or runtime state. Originals must remain in the private server backup
and rollback locations; this hash inventory alone is not a restorable backup.

`development_eventbus.py` emits an evidence-only migration proposal with a fresh
Dev run ID. It does not produce executable Compose or start containers. Never
deploy a shadow Compose by replacing its environment name: old bind mounts,
external volumes and networks can survive that replacement. A fresh runtime
must be built from explicit image identities and independently reviewed resource
definitions, then verified against actual Docker mounts and networks.

The Staging JSONL sanitizer is not a complete database snapshot/import tool.
Passing its tests does not establish production data sanitization, relational
integrity, database identity, login acceptance or 75-user capacity.

For existing isolated databases, run `database_identity.py measure --environment
staging` (or `development`), preserve its result outside the environment folder,
then `apply` and `verify` with the same environment. The command checks Docker
project and environment labels, networks, exclusive database volumes (including
stopped containers), all eight database names and the existing OnlineData marker.
It creates only missing marker tables; an existing empty or conflicting marker
fails closed. It never replaces an existing marker or copies business data.
MySQL DDL commits independently; preserve failed-run evidence and remeasure after
failure. A partially created empty marker is deliberately not overwritten.
Deploy the backend's all-eight-domain startup check only after this verification.
