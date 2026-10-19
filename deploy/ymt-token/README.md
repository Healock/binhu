# YMT token issuer

This is an isolated FastAPI service for `47.100.44.36`. It signs five-minute
Ed25519 tokens and has no Binhu imports, database connection, upstream proxy,
or CORS configuration.

The issuer defaults to `kid=zwzz-2026-02` and issuance disabled. Provision the
key locally on 47, distribute only its public key, configure the Binhu verifier
and community scope, then explicitly set `YMT_TOKEN_ISSUANCE_ENABLED=true`
in the systemd service. While disabled, token requests return
`503 SIGNING_UNAVAILABLE`; health still checks the signing key. Use
`YMT_TOKEN_KID` for subsequent key rotations.

Install the service under `/opt/ymt-token`, create the `ymt-token` system user,
and install the unit as `/etc/systemd/system/ymt-token.service`. The private
key and build allowlist are operator-managed files with mode `0600`:

- `/etc/ymt/signing-private.pem` (PKCS#8 Ed25519 private key)
- `/etc/ymt/builds.json` (build SHA-256 keys, version, `comm`, `communities`, and `enabled`)

`POST /ymt/token` accepts only `{ "build": "<64 lowercase hex characters>" }`.
The build must be listed and enabled in `builds.json`; otherwise the service
returns `403 BUILD_NOT_ALLOWED`. Issued tokens use `sub=build:<hash>`.
The service limits each source IP to 60 requests per minute and the whole
service to 20,000 requests per rolling 24 hours. Quotas persist across restarts
in `/var/log/ymt-token/rate.sqlite3`; this directory must be writable by the
service user. A quota-store failure rejects issuance with 503.

Example `builds.json` (replace the example hash, community code, and name):

```json
{"builds":{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":{"version":"3.42.0","comm":["TEST_COMMUNITY"],"communities":[{"code":"TEST_COMMUNITY","name":"示例社区"}],"enabled":true}}}
```

For each build, `communities` must map every `comm` code to exactly one
non-empty display name. Duplicate codes, missing mappings, and mappings outside
the token's authorized `comm` list fail closed. The response returns this list
as `communities`; the signed JWT continues to contain only authorized codes in
`comm`. Existing non-empty community entries must be updated with their names
when this version is deployed.

The top-level hash-to-entry mapping is also accepted. Replace the file
atomically to update allowed builds without restarting. Disabling a build stops
new issuance immediately; already-issued tokens expire after five minutes
(plus the verifier's existing clock tolerance).

The Nginx locations in `nginx-location.conf` must be added inside the existing
HTTPS server block through the approved gateway. The existing exact-match
`/ymt/ymt-latest.js` location must remain unchanged. The service listens only
on `127.0.0.1:48728`.

Do not put either secret file in Git, an image, or environment variables.
