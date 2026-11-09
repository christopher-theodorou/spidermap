# Releasing a new version

A short checklist so every release is complete and safe. Follow it top to bottom.

## Version numbers

SpiderMap uses `MAJOR.MINOR.PATCH`. While it is `0.x`:

| Change | Example | Bump |
|---|---|---|
| Bug fix only | the data folder was not created | patch: 0.2.2 → 0.2.3 |
| New feature, or a change in behaviour | rooms, floor plan, Zabbix module | minor: 0.2.x → 0.3.0 |
| Security fix | anything that limits access | at least patch, and say **"Security - please upgrade"** in the release |

`1.0.0` once SpiderMap is stable and tested on more than one network.

The version lives in **two** places, keep them equal:
- `__version__` in `spidermap.py`
- `PAGE_VERSION` in `spidermap.html` (this also makes open pages reload themselves after an update)

## Before the release

1. Bump the version in both places.
2. Add a section to `CHANGELOG.md`: *Added / Changed / Fixed*, plus *Upgrade notes* when something needs a
   manual step.
3. Update `README.md` if a feature, setting, requirement or security behaviour changed.
   Update `docs/ARCHITECTURE.md` if files, paths, jobs or the gateway changed.
4. Run the checks:
   ```bash
   python3 -m py_compile spidermap.py spidermap_ports.py spidermap_zabbix.py spidermap_cli.py
   bash -n install.sh
   python3 tests/test_core.py              # must end with "N/N passed"
   python3 tests/test_ports.py             # must end with "N/N passed"
   python3 tests/test_gateway.py           # must end with "N/N passed"
   python3 tests/test_cli.py               # must end with "N/N passed"
   ```
5. Make sure nothing personal is in the files: no real MAC addresses, IPs, switch names, tokens or screenshots of
   your own network.

## Publishing

1. Upload **all** changed files in **one** commit, with a message that says what changed
   (e.g. `SpiderMap 0.3.0: rooms and floor plan`). Folders (`docs/`, `tests/`, `assets/`) are uploaded by dragging
   the folder itself.
2. Check on GitHub that every file shows the new commit, and that the README looks right.
3. **Releases → Draft a new release**: tag `vX.Y.Z` on `main`, title `SpiderMap X.Y.Z` (add ` – Security update`
   for security releases), paste the CHANGELOG section, put the upgrade command in a code block:
   ````
   ```bash
   cd /opt/spidermap && git pull && bash install.sh --update
   ```
   ````
4. Tick **Set as the latest release** and publish.

## After the release: test on a real server

```bash
cd /opt/spidermap && git pull && bash install.sh --update
python3 spidermap.py --version                                              # new version
systemctl is-active spidermap                                               # active
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/zabbix/spidermap.php     # 401
curl -s http://127.0.0.1/zabbix/spidermap.php?data=map                       # {"ok":false,"error":"login"}
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/zabbix/spidermap.json    # 403 or 404
tail -n 5 /var/lib/spidermap/spidermap.log                                   # no errors
```

In the browser: logged in as Super admin the map loads; in a private window you get the login screen.

If something is wrong: fix it and release a patch version. Users can go back to the previous version with
`git checkout vX.Y.Z && bash install.sh --update`.
