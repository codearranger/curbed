# curbed redirect

Point a recovered Curb Energy hub at your own server. Run this **after** the
root-access payload (`serve.py` + the update) has given you SSH on the hub.

It reconfigures the hub's `lamarr streamer` so its sample and diagnostic POSTs
go to a server you control instead of the dead `border.prod.energycurb.com`.

This is deliberately **not** part of the GPG update payload: that payload is
universal and shared across every owner, so site-specific redirect settings
(your host, IP, or cert) must stay out of it. Access first, redirect second.

## Usage

```
pip install paramiko
python3 redirect.py --host <curb-ip> [--password curb123] <strategy...>
```

Strategies (combine as needed):

| flag | effect |
|------|--------|
| `--insecure` | disable TLS cert verification in the streamer so a self-signed / local cert is accepted (simplest when you redirect the hub's DNS to your box) |
| `--ca-cert FILE` | install FILE as a trusted CA on the hub and point the streamer at it (keeps verification on) |
| `--endpoints-base URL` | rewrite the hub's endpoints to `URL/v3/<name>`, e.g. `http://192.168.1.50:8080`, for when you do not redirect DNS |
| `--freeze-config` | raise the local config revision so a server-delivered config will not override your endpoints |

Lifecycle: `--status`, `--dry-run`, `--restore` (revert every change), and
`--restart` / `--no-restart` / `--reboot`.

Everything is idempotent and backs up to `<file>.curbed.bak` before the first
edit. Credentials default to `root` / `curb123` (what the payload sets).

## Hub notes

The hub is old BusyBox, so this tool avoids `pkill`/`pgrep` and SFTP (neither
exists there): it writes files via base64 over the shell and restarts the
streamer by finding its PID with `ps` and `kill`.
