# Flink CDC POC operator

This directory is an isolated, synthetic-only Flink CDC smoke environment. It
does not connect to Binhu production or any external business data source.
The compose project, network, database, volumes and run marker are fixed to
`binhu-flink-poc-20260906` / `FlinkPOC_20260906` so an operator typo fails
closed instead of selecting another environment.

Run on the target host, after reviewing the generated files:

```text
python3 operator.py prepare
# Edit .env: set FLINK_IMAGE to a verified @sha256 image and CDC_SHA256 to
# the verified SHA256 of the Maven Central MySQL CDC connector JAR.
python3 operator.py config
python3 operator.py up
python3 operator.py verify
python3 operator.py smoke
python3 operator.py stop
```

`prepare` creates random mode-0600 credentials, a unique `POC_RUN_ID`, a
synthetic identity marker and a rendered MySQL init script. It refuses to
overwrite any of them. `config`, `up`, `verify`, `smoke` and `stop` all read
and validate the marker before acting. `verify` additionally checks the
Compose network labels, that exactly the three expected services are running,
and that the MySQL identity row matches the current run. `smoke` uses a
non-TTY exec and reads the CDC password from the generated `.env`; it never
prints that password.

The Flink services build one local runtime image from a base image pinned by
digest and a MySQL CDC JAR whose SHA256 is checked during the image build.
The build therefore needs target-host network access to Maven Central, but the
runtime services publish no ports and join only the internal POC network.
The `mysql-cdc` SQL job is only an infrastructure smoke test for binlog
capture. It is not a business-derived consumer contract: any future address,
tag, task-graph or report job must read through the versioned
`/internal/v1/derived-input` interface and must not add its own MySQL SQL.
`stop` retains named volumes for diagnosis. There is intentionally no cleanup
command in this POC operator; deleting volumes requires a separately reviewed
maintenance action.

The Windows `verify.ps1` helper delegates to `operator.py` so it cannot drift
into a second database or credential contract. Docker/real MySQL validation is
not run from this development checkout; perform it only on the designated
isolated target host after this static review.
