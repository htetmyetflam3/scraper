# Debian 12: keep a run alive after SSH disconnects

Run as root from the repository on the VPS, with `uv` on your PATH and your
usual `.env` and input files already configured:

```sh
python3 deploy/install_service.py -- --process -l 2 -i 60
systemctl start scraper.service
systemctl status scraper.service
journalctl -u scraper.service -f
```

Replace everything after `--` with your normal `main.py` arguments, for example
`--process 'https://example.com' -l 2`, `--download`, or `--phatmal`.
Arguments are passed literally, not evaluated as shell commands. Do not put
passwords in flags: the installed unit is readable; keep credentials in `.env`.
The installer records absolute paths to this checkout, `uv` and the `node`
found at install time; do not move those paths while using the service.
Existing scraping and email logic is unchanged.

## The JS engine starts with the same command

One `systemctl start scraper.service` is the whole automated process. Before
`main.py` runs, the unit runs `deploy/engine_ready.py --fix` (ExecStartPre),
which:

1. finds `node` and requires version 18 or newer;
2. checks `Engine/MonoSyllabism/map/{payload.jsc,loader.js}` and the
   precomputed `Engine/.cache-tree/`;
3. boots the real bridge (`crawler/bin/segment_bridge.mjs`) with empty input,
   which loads the encrypted payload through the loader, the syllable tree and
   the formatter;
4. if that boot fails because the V8 bytecode was compiled by a different
   Node, runs `npm run rebuild` once (the same command from INSTALL.md) and
   retries.

If any step still fails the unit does **not** start `main.py`; the reason is in
`journalctl -u scraper.service`. Run the check by hand any time:

```sh
uv run deploy/engine_ready.py          # check only
uv run deploy/engine_ready.py --fix    # check and rebuild the bytecode if needed
```

The unit's `Environment=PATH=` contains the Node directory found at install
time, because systemd does not read your shell profile (an nvm-only Node is
otherwise invisible). `TimeoutStartSec=1800` allows a first-start rebuild
(`npm install` in `Engine/MonoSyllabism/mapbuilder` included).

The segmentor is spawned by the run at flush time; it is not a separate
daemon, and nothing needs to stay running between runs.

Once started, you can close SSH. Ctrl+C while following the journal only stops
viewing logs, not the scraper. `--process` still ends at its configured six-hour
mark (or earlier if finished/failed); systemd does not restart it automatically.
A completed job shows as inactive; inspect its exit result and recent logs with:

```sh
systemctl show scraper.service -p Result -p ExecMainStatus
journalctl -u scraper.service -n 100 --no-pager
```

Stop intentionally with `systemctl stop scraper.service` (SIGINT, matching the
CLI's pause handling; forced termination after 120 seconds). To change flags,
stop first, rerun the installer, then start again. Only one job runs through
this service; avoid also starting the same scraper manually.

Boot startup is **off by default**, so rebooting won't unexpectedly start another
six-hour run. If wanted, `systemctl enable scraper.service` starts a fresh run
on each boot; undo with `systemctl disable scraper.service`.
A service cannot keep running while the VPS is powered off.

Remove the service (keeps downloads and configuration):

```sh
systemctl disable --now scraper.service
rm /etc/systemd/system/scraper.service
systemctl daemon-reload
```
