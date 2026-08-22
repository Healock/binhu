# Binhu update server

These files deploy an isolated desktop-update service on `47.100.44.36`. They
do not modify the platform containers, database or existing application site.
The installer supports Debian/Ubuntu Nginx layouts and the BT Panel layout
used by the production Alibaba Cloud Linux server. On BT Panel it preserves the
existing port 80 site, adds only an ACME challenge include, and reserves a
separate port 443 virtual host for update downloads.

## 1. Inspect first

Copy this directory to the server and run:

```sh
sudo ./install-server.sh --check
```

Review the operating system, free disk, listening ports and the captured Nginx
configuration before making changes. Port `80` is needed for ACME validation,
port `443` serves updates, and the existing SSH daemon must listen on `51234`.

Before adding GitHub secrets, verify the ED25519 host-key fingerprint from the
server console. The expected value recorded for this project is:

```text
SHA256:YlKDIaF4ugAEz52NpFCAxmBxTeYfWoCxhND9pPJRYCw
```

## 2. Install the restricted publisher

Create a dedicated ED25519 key pair for the `desktop-production` GitHub
Environment. Pass only its public key to the installer:

```sh
sudo ./install-server.sh --install 'ssh-ed25519 AAAA... binhu-actions'
```

This creates `/srv/binhu-updates`, the restricted `binhu-update-publish`
account, the forced SSH gateway, an HTTP-only ACME site, and the
certificate-renewal timer definition. The account has a valid POSIX shell only
so OpenSSH can start the forced command; the key itself is restricted and
cannot open an interactive shell, forward ports or browse files.

The gateway accepts only:

```text
status
fetch <win7-x64|win10-x64> <current-full-package.nupkg>
publish <version> <40-char-commit> <byte-length> <sha256>
```

`fetch` is read-only and can return only the full package named by the current
platform state. It cannot read arbitrary files, archived releases or anything
outside `public/<platform>/`. CI uses it to obtain the previous full package
over the restricted SSH channel when a hosted runner cannot reach the HTTPS
download endpoint. Upload bodies are validated for length, SHA-256, SemVer,
commit ID, safe paths, allowed names and Velopack feed/package consistency.
Files are installed before `releases.stable.json` is replaced atomically.
Version `0.25.15` must be full-only; later versions must contain a current delta.
The latest five release sets remain public and older files move to
`/srv/binhu-updates/archive`.

## 3. Obtain the IP certificate

Python `3.9` or newer and Certbot `5.4` or newer are required. After port `80`
reaches this server, run:

On Alibaba Cloud Linux 3, keep Certbot isolated from the system Python:

```sh
sudo dnf install -y python3.11 python3.11-pip
sudo python3.11 -m venv /opt/binhu-certbot
sudo /opt/binhu-certbot/bin/python -m pip install 'certbot==5.4.0'
sudo ln -sfn /opt/binhu-certbot/bin/certbot /usr/local/bin/certbot
```

Then request and activate the certificate:

```sh
sudo binhu-obtain-ip-certificate security@example.invalid
```

Replace the example with the real ACME contact address. The command requests
the short-lived certificate for `47.100.44.36`, verifies the files, switches
Nginx from the HTTP bootstrap site to the HTTPS update site, and enables the
twice-daily renewal timer.

The renewal job uses `certbot renew`, reloads Nginx only after a successful
certificate deployment, and logs a critical syslog message after two
consecutive failures. Production monitoring must route that message to an
operator.

## 4. Configure GitHub

Create the `desktop-production` Environment and add:

```text
BINHU_UPDATE_SSH_KEY
BINHU_UPDATE_KNOWN_HOSTS
```

`BINHU_UPDATE_KNOWN_HOSTS` must be the verified `[47.100.44.36]:51234` host-key
line. The workflow fixes the host, port and user in source code and sends one
validated tar stream over SSH standard input.

## 5. Verify

After the first publish, check:

```text
https://47.100.44.36/updates/win7-x64/releases.stable.json
https://47.100.44.36/updates/win10-x64/releases.stable.json
```

Feeds and `policy.stable.json` use `Cache-Control: no-store`. Versioned setup,
full and delta packages use immutable caching and support range requests.
