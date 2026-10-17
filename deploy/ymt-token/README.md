# YMT token issuer

This is an isolated FastAPI service for `47.100.44.36`. It signs five-minute
Ed25519 tokens and has no Binhu imports, database connection, upstream proxy,
or CORS configuration.

Install the service under `/opt/ymt-token`, create the `ymt-token` system user,
and install the unit as `/etc/systemd/system/ymt-token.service`. The private
key and build allowlist are operator-managed files with mode `0600`:

- `/etc/ymt/signing-private.pem` (PKCS#8 Ed25519 private key)
- `/etc/ymt/builds.json` (build SHA-256 keys, version, `comm`, and `enabled`)

`POST /ymt/token` accepts only `{ "build": "<64 lowercase hex characters>" }`.
The build must be listed and enabled in `builds.json`; otherwise the service
returns `403 BUILD_NOT_ALLOWED`. Issued tokens use `sub=build:<hash>`.
The service limits each source IP to 60 requests per minute and the whole
service to 20,000 requests per UTC day.

The Nginx locations in `nginx-location.conf` must be added inside the existing
HTTPS server block through the approved gateway. The existing exact-match
`/ymt/ymt-latest.js` location must remain unchanged. The service listens only
on `127.0.0.1:48728`.

Do not put either secret file in Git, an image, or environment variables.
