#!/usr/bin/env python3
"""
curbed redirect — point a recovered Curb Energy hub at your own server.

Run this AFTER the curbed root-access payload (serve.py + setup.sh) has given
you SSH on the hub. It reconfigures the hub's `lamarr streamer` so its sample
and diagnostic POSTs go to a server you control, instead of the dead
border.prod.energycurb.com cloud.

It does NOT belong in the GPG update payload: that payload is universal and
shared, so site-specific redirect settings (your host/IP/cert) must stay out of
it. Access first (the payload), then redirect (this tool).

Strategies (combine as needed):
  --insecure            Disable TLS cert verification in the streamer so a
                        self-signed / local cert is accepted. Simplest when you
                        already redirect the hub's DNS to your box.
  --ca-cert FILE        Install FILE as a trusted CA on the hub and point the
                        streamer's curl at it (keeps verification ON).
  --endpoints-base URL  Rewrite the hub's endpoints to URL + /v3/<name>, e.g.
                        https://curb.lan  or  http://192.168.1.50:8080 .
                        Use this when you do NOT redirect DNS.
  --freeze-config       Raise the local config revision so a server-delivered
                        config will not silently override your endpoints.

Lifecycle:
  --status     Show redirect-relevant state and exit.
  --dry-run    Print the planned changes; write nothing.
  --restore    Revert every change this tool made (from .curbed.bak backups).
  --restart / --no-restart / --reboot   How to apply changes (default --restart).

Everything is idempotent and backed up to <file>.curbed.bak before first edit.

Requires: paramiko (pip install paramiko). The hub's default post-recovery
credentials are root / curb123.
"""

import argparse
import json
import sys

try:
    import paramiko
except ImportError:
    sys.exit("This tool needs paramiko:  pip install paramiko")

STREAMER = "/data/lamarr/streamer.lua"
CONFIG = "/data/hub-config.json"
CA_ON_HUB = "/data/curbed-ca.pem"
CURL_INIT = "local curl = cURL.easy_init()"
BEGIN = "-- >>> curbed-redirect >>>"
END = "-- <<< curbed-redirect <<<"
ENDPOINT_NAMES = ("samples", "diagnostics", "messages", "hub_config")
MAX_REVISION = 2000000000  # for --freeze-config


class Hub:
    def __init__(self, host, user, password, port):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(host, port=port, username=user, password=password,
                       timeout=10, auth_timeout=10, banner_timeout=12,
                       allow_agent=False, look_for_keys=False)

    def run(self, cmd, stdin_data=None):
        i, o, e = self.c.exec_command(cmd, timeout=30)
        if stdin_data is not None:
            i.write(stdin_data)
            i.flush()
            i.channel.shutdown_write()
        out = o.read().decode(errors="replace")
        err = e.read().decode(errors="replace")
        rc = o.channel.recv_exit_status()
        return rc, out, err

    def read(self, path):
        rc, out, err = self.run("cat %s" % path)
        if rc != 0:
            raise IOError("cannot read %s: %s" % (path, err.strip()))
        return out

    # Old Dropbear on the hub has no SFTP subsystem, so push bytes through the
    # shell, base64-encoded (busybox base64 -d), to survive arbitrary content.
    def _write_bytes(self, path, data):
        import base64
        b64 = base64.b64encode(data).decode()
        rc, _, err = self.run("base64 -d > %s" % path, stdin_data=b64)
        if rc != 0:
            raise IOError("write %s failed: %s" % (path, err.strip()))

    def write(self, path, content):
        self._write_bytes(path, content.encode())

    def put(self, local, remote):
        with open(local, "rb") as f:
            self._write_bytes(remote, f.read())

    def backup(self, path, dry):
        bak = path + ".curbed.bak"
        rc, _, _ = self.run("test -f %s" % bak)
        if rc == 0:
            return "backup exists: %s" % bak
        if dry:
            return "WOULD back up %s -> %s" % (path, bak)
        self.run("cp -a %s %s" % (path, bak))
        return "backed up %s -> %s" % (path, bak)

    def close(self):
        self.c.close()


def patch_streamer(text, insecure, cainfo):
    """Return patched streamer.lua text with a single marked curbed block."""
    lines = []
    if insecure:
        lines += ["curl:setopt_ssl_verifypeer(false)",
                  "curl:setopt_ssl_verifyhost(false)"]
    if cainfo:
        lines += ["curl:setopt_cainfo('%s')" % cainfo]
    block = "\n".join([BEGIN] + lines + [END])

    # Remove any existing curbed block first (idempotent).
    if BEGIN in text and END in text:
        pre, rest = text.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        text = pre.rstrip("\n") + "\n" + post.lstrip("\n")

    if not lines:
        return text  # nothing to insert (e.g. only restoring)

    if CURL_INIT not in text:
        raise RuntimeError("could not find curl init line in streamer.lua")
    return text.replace(CURL_INIT, CURL_INIT + "\n" + block, 1)


def rewrite_endpoints(cfg_text, base, freeze):
    cfg = json.loads(cfg_text)
    base = base.rstrip("/")
    eps = cfg.setdefault("endpoints", {})
    changes = {}
    for name in ENDPOINT_NAMES:
        new = "%s/v3/%s" % (base, name)
        if eps.get(name) != new:
            changes[name] = (eps.get(name), new)
            eps[name] = new
    if freeze:
        changes["revision"] = (cfg.get("revision"), MAX_REVISION)
        cfg["revision"] = MAX_REVISION
    return json.dumps(cfg), changes


def restart(hub, reboot, dry):
    if reboot:
        if dry:
            return "WOULD reboot the hub"
        hub.run("sync; (sleep 1; reboot) >/dev/null 2>&1 &")
        return "rebooting hub"
    if dry:
        return "WOULD restart streamer (kill its PID; supervisor respawns it)"
    # busybox on the hub has no pkill/pgrep, so find the PID via ps.
    rc, out, _ = hub.run(
        "ps w | grep streamer.lua | grep luajit | grep -v grep | awk '{print $1}'")
    pids = out.split()
    if not pids:
        return "streamer not running; supervisor should start the patched one"
    hub.run("kill " + " ".join(pids))
    return "restarted streamer (killed pid %s; supervisor respawns it)" % ",".join(pids)


def do_status(hub):
    print("== hub-config.json endpoints ==")
    try:
        cfg = json.loads(hub.read(CONFIG))
        for k, v in cfg.get("endpoints", {}).items():
            print("  %-12s %s" % (k, v))
        print("  revision     %s" % cfg.get("revision"))
    except Exception as e:
        print("  (error: %s)" % e)
    print("== streamer TLS patch ==")
    txt = hub.read(STREAMER)
    if BEGIN in txt:
        body = txt.split(BEGIN, 1)[1].split(END, 1)[0].strip()
        print("  curbed block PRESENT:")
        for ln in body.splitlines():
            print("    " + ln)
    else:
        print("  no curbed block (stock verification)")
    rc, out, _ = hub.run("ps w | grep '[s]treamer.lua'")
    print("== streamer process ==\n  " + (out.strip() or "(not running)"))


def do_restore(hub, dry):
    actions = []
    for path in (STREAMER, CONFIG):
        bak = path + ".curbed.bak"
        rc, _, _ = hub.run("test -f %s" % bak)
        if rc == 0:
            actions.append("%s restore %s <- %s" %
                           ("WOULD" if dry else "", path, bak))
            if not dry:
                hub.run("cp -a %s %s" % (bak, path))
        else:
            actions.append("no backup for %s (skipped)" % path)
    rc, _, _ = hub.run("test -f %s" % CA_ON_HUB)
    if rc == 0 and not dry:
        hub.run("rm -f %s" % CA_ON_HUB)
        actions.append("removed %s" % CA_ON_HUB)
    return actions


def main():
    ap = argparse.ArgumentParser(description="Redirect a recovered Curb hub to your own server.")
    ap.add_argument("--host", required=True, help="hub IP/hostname")
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="curb123")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--insecure", action="store_true", help="disable TLS cert verification in the streamer")
    ap.add_argument("--ca-cert", metavar="FILE", help="install FILE as trusted CA and use it (keeps verification on)")
    ap.add_argument("--endpoints-base", metavar="URL", help="rewrite endpoints to URL + /v3/<name>")
    ap.add_argument("--freeze-config", action="store_true", help="raise local config revision so server config won't override")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--restart", dest="restart", action="store_true", default=True)
    g.add_argument("--no-restart", dest="restart", action="store_false")
    g.add_argument("--reboot", action="store_true")
    args = ap.parse_args()

    hub = Hub(args.host, args.user, args.password, args.port)
    try:
        if args.status:
            do_status(hub)
            return
        if args.restore:
            for a in do_restore(hub, args.dry_run):
                print(a)
            print(restart(hub, args.reboot, args.dry_run))
            return

        if not (args.insecure or args.ca_cert or args.endpoints_base):
            ap.error("choose at least one of --insecure / --ca-cert / --endpoints-base (or --status/--restore)")

        cainfo = CA_ON_HUB if args.ca_cert else None

        # Install CA first, if requested.
        if args.ca_cert:
            if args.dry_run:
                print("WOULD upload %s -> %s" % (args.ca_cert, CA_ON_HUB))
            else:
                hub.put(args.ca_cert, CA_ON_HUB)
                hub.run("chmod 644 %s" % CA_ON_HUB)
                print("installed CA -> %s" % CA_ON_HUB)

        # Patch streamer.lua (TLS verify / cainfo).
        if args.insecure or args.ca_cert:
            print(hub.backup(STREAMER, args.dry_run))
            orig = hub.read(STREAMER)
            patched = patch_streamer(orig, args.insecure, cainfo)
            if patched != orig:
                if args.dry_run:
                    print("WOULD patch streamer.lua curl block:")
                    print("  " + "\n  ".join(
                        patched.split(BEGIN, 1)[1].split(END, 1)[0].strip().splitlines()))
                else:
                    hub.write(STREAMER, patched)
                    print("patched streamer.lua TLS settings")
            else:
                print("streamer.lua already in desired state")

        # Rewrite endpoints.
        if args.endpoints_base or args.freeze_config:
            print(hub.backup(CONFIG, args.dry_run))
            cfg_text = hub.read(CONFIG)
            base = args.endpoints_base
            if base is None:
                # freeze only: keep endpoints, bump revision
                cfg = json.loads(cfg_text)
                changes = {"revision": (cfg.get("revision"), MAX_REVISION)}
                cfg["revision"] = MAX_REVISION
                new_text = json.dumps(cfg)
            else:
                new_text, changes = rewrite_endpoints(cfg_text, base, args.freeze_config)
            for k, (old, new) in changes.items():
                print("  %s: %s -> %s" % (k, old, new))
            if changes and not args.dry_run:
                hub.write(CONFIG, new_text)
                print("wrote hub-config.json")

        print(restart(hub, args.reboot, args.dry_run))
        print("\nDone. Watch your server for POSTs to /v3/samples; the hub will"
              "\nbackfill its buffered samples once it connects successfully.")
    finally:
        hub.close()


if __name__ == "__main__":
    main()
