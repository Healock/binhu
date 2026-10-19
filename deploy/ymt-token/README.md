# YMT token issuer

This is an isolated FastAPI service for `47.100.44.36`. It signs five-minute
Ed25519 tokens and has no Binhu imports, database connection, upstream proxy,
or CORS configuration.

The issuer defaults to `kid=zwzz-2026-02` and issuance disabled. The JWT signing
key and the publisher's release-verification key are separate keys with
separate purposes. Provision the JWT private key locally on 47, install the
release public key from `release-public.pem`, set the release policy and
community scope, then explicitly set `YMT_TOKEN_ISSUANCE_ENABLED=true` in the
systemd service. While disabled, token requests return `503
SIGNING_UNAVAILABLE`; health checks both the JWT signing key and the signed
release files. Use `YMT_TOKEN_KID` for subsequent JWT key rotations.

Install the service under `/opt/ymt-token`, create the `ymt-token` system user,
and install the unit as `/etc/systemd/system/ymt-token.service`. The JWT
private key is `ymt-token:ymt-token` mode `0600`. The allowlist, release policy,
and release public key are root-owned and readable by `ymt-token` (for example
`root:ymt-token` mode `0640`):

- `/etc/ymt/signing-private.pem` (PKCS#8 Ed25519 private key)
- `/etc/ymt/builds.json` (legacy build SHA-256 keys, version, `comm`, `communities`, and `enabled`)
- `/etc/ymt/release-public.pem` (pinned publisher Ed25519 public key; source: `deploy/ymt-token/release-public.pem`)
- `/etc/ymt/release-policy.json` (minimum version and server-owned community scope)
- `/var/www/ymt/release.json` and `/var/www/ymt/ymt-latest.js` (signed release manifest and matching artifact)

`POST /ymt/token` accepts only `{ "build": "<64 lowercase hex characters>" }`.
The service verifies the local `release.json` signature with the pinned
publisher key and checks that the manifest hash and size match the local
`ymt-latest.js` bytes. A matching, valid release at or above
`minimum_version` can receive a token without adding its hash to
`builds.json`. Other builds must match an enabled legacy allowlist entry whose
version is also at or above the floor; unknown, disabled, or too-old builds
return `403 BUILD_NOT_ALLOWED`. Invalid release files fail closed with
`503 SIGNING_UNAVAILABLE`. Issued tokens use `sub=build:<hash>`.
The service limits each source IP to 60 requests per minute and the whole
service to 20,000 requests per rolling 24 hours. Quotas persist across restarts
in `/var/log/ymt-token/rate.sqlite3`; this directory must be writable by the
service user. A quota-store failure rejects issuance with 503.

Legacy `builds.json` example (replace the example hash, community code, and name):

```json
{"builds":{"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":{"version":"3.42.0","comm":["TEST_COMMUNITY"],"communities":[{"code":"TEST_COMMUNITY","name":"示例社区"}],"enabled":true}}}
```

For each build, `communities` must map every `comm` code to exactly one
non-empty display name. Duplicate codes, missing mappings, and mappings outside
the token's authorized `comm` list fail closed. The response returns this list
as `communities`; the signed JWT continues to contain only authorized codes in
`comm`. Existing non-empty community entries must be updated with their names
when this version is deployed.

Example `/etc/ymt/release-policy.json`:

```json
{"minimum_version":"3.43.0","comm":["REPLACE_WITH_COMMUNITY_CODE"],"communities":[{"code":"REPLACE_WITH_COMMUNITY_CODE","name":"示例社区"}]}
```

Raise `minimum_version` as releases are promoted; do not lower it. The floor
applies to signed releases and legacy allowlist entries. The manifest signature
covers format, version, SHA-256, and byte count; `issued` is informational and
is not part of the signed message. A release rollout must install the JS and
manifest as a matching pair before restarting/reloading the service, because
the issuer verifies both on each request.

The top-level hash-to-entry mapping is also accepted. Replace the file
atomically to update allowed builds without restarting. Disabling a build stops
new issuance immediately; already-issued tokens expire after five minutes
(plus the verifier's existing clock tolerance).

The Nginx locations in `nginx-location.conf` must be added inside the existing
HTTPS server block through the approved gateway. The existing exact-match
`/ymt/ymt-latest.js` location must remain unchanged. The service listens only
on `127.0.0.1:48728`.

Never put either private signing key or runtime policy/allowlist files in Git,
an image, or environment variables. The release verification public key is
non-secret and is intentionally versioned in this directory.
