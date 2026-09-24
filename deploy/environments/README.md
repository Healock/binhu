# Binhu environment provisioning

This directory provisions the isolated `development` and `staging` projects.
It never reuses production volumes and never accepts a production database as a
runtime target. Run `python provision.py preview --environment <development|staging>`
before `apply`; `apply` requires an explicit server maintenance window.

Dev is intended for Kafka/Redis/Flink integration; Staging is intended for a
sanitized production copy and load testing. These roles are acceptance targets,
not evidence that either data pipeline has been verified.

## Shared frontend artifact

Build the Dev/Staging frontend once with `npm run build -- --mode environment`
into `frontend/dist-environment/` (ignored by Git and Tailwind source scanning).
This emits relative resource URLs. Promote that exact directory and its hashes
alongside the same backend image; do not rebuild separately for Staging. The
backend supplies only the fixed HTML base (`/dev/` or `/staging/`) at response
time. On-disk HTML, JS, CSS, lazy chunks and public startup scripts stay identical.
Normal production, desktop and Android build modes retain their existing base.

`runtime.py prepare` validates the bundle before creating environment files.
The backend returns a non-cacheable structured 503 with
`environment_static_bundle_invalid` when given an old root-based bundle; it
does not fetch assets from Production. Updating existing environments therefore
requires switching the compatible backend and frontend together. Preserve the
old image/static directory for a paired rollback. Verify root, login and nested
task URLs, lazy-loaded pages, and all resource requests under each fixed prefix.
This asset fix alone is not the artifact-promotion or environment-readiness gate.

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

## Fixed Staging promotion gateways

Staging application and sanitized data promotion use two separate SSH accounts,
forced-command wrappers and sudo allowlists. Install them only through
`install-staging-promotion-gateways.yml` after the exact main revision passes CI.
The installer creates `binhu-staging-app-deploy` and `binhu-staging-data-deploy`;
neither key is shared with Production, Development or the Staging event-pipeline.
All three workflows require the fixed `BINHU_STAGING_HOST` and
`BINHU_STAGING_PORT` secrets; the port is validated as an integer from 1 through
65535 and is never accepted as a workflow input or shell fragment.

The application account accepts only:

```text
prepare <staging-app-run-id> <40-hex-main-commit> <version> <artifact-id> <dev-update-run-id>
measure <run-id> <artifact-id>
apply <run-id> <artifact-id>
accept <run-id> <artifact-id> <dev-update-run-id>
```

`prepare` accepts an archive with exactly `artifact.json`, `source.tar` and
`frontend.tar`, verifies that its ID is the already accepted Dev artifact, and
adopts the exact Backend image ID from the bound Dev update record without a
Docker build. It also verifies that image's labels and full `/app` file hashes.
`measure`, `apply` and `accept` each require the successful Dev update record and
the live Dev manifest to continue containing those exact IDs before Staging can
advance.
Every step records commit, version, artifact ID, image ID, time and safe outcome.

The data account accepts only `status`, `measure`, the fixed
`export approved-sanitized-scope-v1`, and `create/import/verify/switch` with a
`staging-<16hex>` ID. Production access exists only inside the reviewed read-only
exporter. The target actions can address only `Staging_s<id>_*` databases on
`binhu-staging_internal`; there is no path, environment, network, database or
shell argument. Repeated successful import reads the immutable result record and
does not execute inserts again. Arbitrary commands, Production writes, Dev or
Shadow targets and cross-environment network selection are rejected by both the
forced shell and Python boundary.

Private audit logs contain action, fixed run identity, immutable digests, time,
outcome and bounded reason codes only. They never contain database credentials,
passwords, source rows, names, phones, identity numbers, addresses, notes or
driver output. A workflow failure and the corresponding private `*-alert-*.json`
record are the alert channels for switch, rollback and validation failures.
The installer retains `/usr/local/libexec/binhu-staging-promotion/control-commit`
after a successful atomic replacement and restores the previous value only when
installation fails. Snapshot `measure` and `export` allocate a fresh private
`staging-<16hex>` evidence directory before resource and container preflight, so
an early memory, disk or Production identity refusal is attributable to that
attempt instead of being confused with an older failed snapshot. Early evidence
contains only the fixed policy, snapshot ID, phase and bounded reason code.

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

The [Dev metadata pipeline](event_pipeline/README.md) provides a closed task-event
contract, durable delivery ledger, fixed Flink aggregation and revision-fenced
Redis bridge. Its preparation/startup and minimal synthetic acceptance are separate
from schema-registry, recovery and full business integration acceptance.

Before a Dev runtime acceptance, verify the current Bootstrap identity with
`python -m deploy.environments.dev_bootstrap_acceptance verify
--expected-version <candidate-version>`. The verifier uses only the fixed
loopback Dev endpoint. It requires `server_version`, `environment=development`,
`environment_id=development`, `api_entry=/dev/api`, `environment_label=Dev 环境 ·
虚构数据`, and `data_kind=虚构或脱敏开发数据`. Its report contains only these
allowlisted identity values. Do not replace `server_version` with the obsolete
`version` field or `/dev/api` with the obsolete `/dev-api` entry.

## Immutable candidate packages

`python -m deploy.environments.artifact build --repository <checkout> --commit
<full-commit-sha> --output <new-private-directory>` builds the portable frontend
from a Git archive. Local edits and untracked files are excluded. Keep the
reported artifact ID with the developer acceptance record. `verify --output`
checks archive hashes, contained files, VERSION, migration hashes and Git archive
commit metadata. These are consistency checks, not a digital signature: transfer
the expected artifact ID separately through the trusted deployment channel.

On the authorized Linux host, run `python -m deploy.environments.image build
--artifact <package-directory> --expected-artifact-id <recorded-id> --output
<new-private-build-directory>`. Only transfer `artifact.json`, `source.tar` and
`frontend.tar`, not the npm installation or mutable extracted build workspace.
The image tool extracts the archived backend afresh, adds the archived VERSION
and its APP_VERSION setting, and records the resulting immutable Docker image ID.
It then checks image labels, runtime version and every file under `/app` in a
network-disabled container with CPU/memory limits. A failed build retains its
failure marker and cannot be reused. `image verify` repeats these checks without
building another image. Build logs remain in the private output directory.

Before building, check production health and resources. The CLI requires at least
2 GiB available RAM and 8 GiB free in Docker's filesystem; these floors do not
replace ongoing resource monitoring. Building or verifying an image does not
update application containers, initialize databases, accept a Dev run or permit
Staging promotion. Same-image deployment, paired rollback and application
acceptance remain separate gates. Candidate manifests deliberately retain
`ready_for_staging=false` until an independent acceptance workflow is completed.

For the existing Dev application, `python -m deploy.environments.update
measure-development --artifact <package> --image-directory <verified-image-run>
--expected-artifact-id <recorded-id>` validates configuration hashes, isolated
databases, resource availability and the candidate image. `apply-development`
uses the same arguments plus `--evidence
/srv/deploy-backups/environment-triad/dev-update-<16-hex-run-id>` and repeats all
checks under the environment deployment lock. It backs up Dev's eight databases,
configuration and static files before replacing the backend image and static
mount together. MySQL and Redis are not recreated. Failed application startup
restores the previous configuration and image and checks their health; database
backups are never automatically imported. Preserve the failure report if rollback
health fails. The command does not support Staging promotion: that still requires
the full Dev acceptance and Staging data gates.

## Fixed Dev application promotion gateway

The Dev application has a separate promotion boundary from the Dev event-pipeline gateway. Install `install-dev-application-gateway.sh` only from an exact `origin/main` revision. The forced account `binhu-dev-app-deploy` accepts only:

```text
prepare <dev-update-run-id> <40-hex-main-commit> <version> <artifact-id>
measure <run-id> <artifact-id>
apply <run-id> <artifact-id>
accept <run-id> <artifact-id>
```

`prepare` builds and verifies the Backend image on the authorized Dev host. `apply` uses the existing development backup, measure, health and rollback contract. `accept` rechecks the live Dev manifest and health before producing the real `dev-update-*` result consumed by the Staging application gateway. The gateway has no Production, Staging, Shadow, arbitrary command, or caller-selected path operation.

The Dev application workflow reuses only the already-registered Dev event-pipeline host, port and known_hosts secrets because both gateways terminate on the same fixed Dev server. It still uses separate application admin/deploy key pairs and the separate `binhu-dev-app-deploy` account; no event-pipeline private key is reused.
