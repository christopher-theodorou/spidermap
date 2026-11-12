# SpiderMap standalone (preview)

SpiderMap outside Zabbix: its own web server and users, in Docker, on any Linux host. It reads Zabbix through a
**read-only** API token and installs nothing on the Zabbix server - no files in the Zabbix frontend, no items,
triggers, dashboards or maps. Proposed and prototyped by Nikolaos Karanikolas.

> **Preview.** The read-only rules and the web server's users and roles are covered by tests
> (`tests/test_readonly.py`). The container build itself is checked by `.github/workflows/standalone.yml`, which
> builds it and runs `standalone/smoke_test.py`. It has not run on a production network yet: try it on a test host
> first.

## How it compares

| | Zabbix-hosted (`install.sh`) | Standalone (this) |
|---|---|---|
| Where the map lives | `spidermap.php` in the Zabbix web folder | its own Apache, outside Zabbix |
| Who may open it | Zabbix users (Zabbix login) | SpiderMap users (bcrypt `htpasswd`), roles viewer / editor / admin |
| What it does in Zabbix | read-only, or also events (your choice) | read-only, always |
| Updating | `spidermap update` | a new image, `docker compose` |

**All users see the same map** - everything the API token may read. Standalone does *not* copy each Zabbix user's
host-group permissions. For different views, run separate instances with separate tokens.

## What it never does in Zabbix

The API client has an allow list of read methods (`host.get`, `item.get`, `trigger.get`, `usermacro.get`,
`image.get`, `history.get`, `trend.get`, `problem.get`, `hostgroup.get`, `template.get`, `apiinfo.version`,
`settings.get`, `dashboard.get`, `map.get`). Every other method is refused **before** the request leaves the
container - also if someone sets `SPIDERMAP_ZABBIX_READONLY=0`: standalone is always read-only. No `history.push`,
no `zabbix_sender`. Activity, speed-test history, settings and your map changes stay in SpiderMap.

Earlier `autotopo.*` objects from a Zabbix-hosted install are not removed automatically; see
[COMMANDS.md](COMMANDS.md#zabbix-read-only-mode).

## Roles

| Role | May |
|---|---|
| `viewer` | see the map |
| `editor` | also rename devices, change icons, start speed tests |
| `admin` | also settings, scans, import, reset |

Roles come from `standalone/auth/roles.json` on the server, never from anything the browser sends. A user without a
known role gets *403*. To revoke access, remove the user from `users.htpasswd`. Basic authentication has no reliable
log-out button: close the browser.

## The Zabbix token

A separate Zabbix user and token, read-only: a user role with *API methods* on **Allow list** `host.get`,
`item.get`, `trigger.get`, `usermacro.get`, `image.get`, `problem.get`, `history.get`, **Read** on the host groups
SpiderMap should map, frontend access disabled. Reading SNMP credentials from macros is still sensitive: prefer a
read-only SNMP account on your switches. *Secret* macros are never returned by the API.

## Install, on a separate Linux host

1. Install Docker Engine and the Compose plugin. Copy the SpiderMap folder to the host and `cd standalone`.
2. `cp .env.example .env` and set:
   - `SPIDERMAP_PUBLIC_ORIGIN` - exactly how browsers reach SpiderMap (scheme, host, port), e.g.
     `https://spidermap.example.net`. Changes are refused from any other origin.
   - `SPIDERMAP_ZABBIX_FRONTEND` - the absolute address of your Zabbix `zabbix.php`, for the *open in Zabbix* links.
3. `docker compose build`
4. Your users (bcrypt):
   ```bash
   docker run --rm -it --entrypoint htpasswd -v "$PWD/auth:/auth" spidermap-standalone:local -cB /auth/users.htpasswd nikos
   docker run --rm -it --entrypoint htpasswd -v "$PWD/auth:/auth" spidermap-standalone:local -B /auth/users.htpasswd eva
   ```
   `-c` only for the first user: it creates a new file.
5. `cp auth/roles.example.json auth/roles.json` and keep only your real users, e.g.
   `{"nikos": "admin", "eva": "editor"}`. No passwords in compose files or git (`.gitignore` covers these files).
6. The Zabbix token, **before** the collector starts - it stays in the `config` volume only:
   ```bash
   docker compose run --rm --no-deps --entrypoint sh init
   umask 077; cat > /etc/spidermap/zabbix.env
   export ZABBIX_URL="https://zabbix.example.net/api_jsonrpc.php"
   export ZABBIX_TOKEN="your-read-only-token"
   ```
   then Ctrl+D and `exit`. For a private CA, mount its certificate; don't switch certificate checks off.
7. `docker compose up -d`, then `docker compose logs init collector web`.
8. Put an **https reverse proxy** (Caddy, nginx) in front of `127.0.0.1:8186`, forwarding `Host` and
   `Authorization`. The web container listens on loopback only. Better still, reach it through a VPN only.
9. Open it, then in *Settings* set the networks to scan, the core switch and any controllers.

The collector uses **host networking** (Linux) for ping and ARP. ARP does not cross routers: from another subnet,
SpiderMap relies on SNMP, Zabbix and controller data. The Ookla speed test is not in the image; the Cloudflare and
Fast.com tests work. Icons are downloaded at `init` when there is internet; otherwise the page uses the online copy.

## Update

The `config` and `runtime` volumes keep your settings and history. Don't run `spidermap update` or `install.sh` in
the containers. With the new SpiderMap folder:

```bash
docker compose build
docker compose stop collector web
docker compose run --rm init
docker compose up -d
```

Never `docker compose down -v` unless you want to delete everything.

## Before trusting it in production

- `docker compose build`, `init`, `apache2ctl -t` and `php -l` succeed (the CI workflow does these).
- Without a password: 401 for the page, data and icons. A forged `X-Auth-User` header gets nothing. Unknown role: 403.
- viewer: map yes, edits / scan / settings / speed test no. editor: edits and speed test yes, settings no.
- A wrong Origin, a missing `X-SpiderMap` header, or a path like `../` are refused. The token and controller
  secrets appear in no page, JSON or log.
- A full scan, live traffic, WAN and a speed test work with the read-only token, and the Zabbix audit log shows
  no changes from SpiderMap.
- A restart keeps map, settings and history; a lost API shows as an error and creates nothing.

Not claimed: high availability, very large networks (thousands of hosts), per-user host filtering.
