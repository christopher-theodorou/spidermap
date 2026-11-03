# Security policy

## Supported versions

Only the latest release gets security fixes. Upgrade with:

```bash
cd /opt/spidermap && git pull && bash install.sh --update
```

| Version | Supported |
|---|---|
| 0.2.2 and newer | ✅ |
| 0.2.0 and older | ❌ the map data or the page could be reached without a Zabbix login - please upgrade |

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Describe what you found, how to reproduce it, and which
version you run. You will get an answer as soon as possible, and credit in the release notes if you wish.

## How SpiderMap protects your network data

- The page and all map data are served only by `spidermap.php`, after checking the Zabbix login through the Zabbix
  API. By default only Zabbix Super admins can open SpiderMap; the guest account is always refused.
- The data lives outside the web folder (`/var/lib/spidermap/www`), readable by root and the web server only.
- Changes (device edits, speed tests) need a Zabbix admin and are protected against cross-site request forgery.
- The Zabbix API token stays in `zabbix.env` (root only) and never reaches the browser.
- SpiderMap only reads from your network; it never changes device configuration.

What SpiderMap does **not** do for you: encrypt the connection (enable HTTPS on Zabbix), or protect a Zabbix that is
published on the internet (use a VPN, or HTTPS plus an extra login and multi-factor authentication).
See *Security* in the [README](README.md#security) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
