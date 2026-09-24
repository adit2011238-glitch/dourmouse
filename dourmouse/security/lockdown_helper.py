"""Dourmouse lockdown helper: the ONLY part of Dourmouse that runs as root
(finding #103). Standalone, standard library only, Python 3.9+ (it runs
under macOS's own /usr/bin/python3).

It does exactly one thing: keep a clearly marked block of /etc/hosts equal
to the list of domain names in one file the user owns, and flush the DNS
cache. launchd runs it whenever that file changes. Every name is validated
against a strict domain pattern, only ``0.0.0.0 name`` / ``:: name`` lines
are ever written, and nothing outside the marked block is touched, so the
most a compromised user process can do through this helper is block
websites. There is no path from the request file to broader root access.

  sudo /usr/bin/python3 lockdown_helper.py --install --request-file PATH
  sudo /usr/bin/python3 /Library/PrivilegedHelperTools/com.dourmouse.lockdown-helper --uninstall
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HOSTS = "/etc/hosts"
BEGIN = "# BEGIN DOURMOUSE LOCKDOWN (managed by the Dourmouse lockdown helper; do not edit)"
END = "# END DOURMOUSE LOCKDOWN"
LABEL = "com.dourmouse.lockdown-helper"
# Root-owned by macOS, the standard place for privileged helpers. NOT
# /usr/local, which Homebrew makes user-writable on many Macs: a root-run
# script in a user-writable directory is a privilege escalation.
INSTALLED = "/Library/PrivilegedHelperTools/com.dourmouse.lockdown-helper"
PLIST = "/Library/LaunchDaemons/%s.plist" % LABEL
MAX_DOMAINS = 5000
MAX_REQUEST_BYTES = 1024 * 1024
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


def valid_domains(raw):
    """Only well-formed lower-case domain names survive; anything else is
    dropped, never written."""
    if not isinstance(raw, list):
        return []
    out = []
    for d in raw[:MAX_DOMAINS]:
        if isinstance(d, str) and DOMAIN_RE.match(d.strip().lower()):
            out.append(d.strip().lower())
    return sorted(set(out))


def block_for(domains):
    names = sorted({n for d in domains for n in (d, "www." + d)})
    lines = [BEGIN]
    for n in names:
        lines.append("0.0.0.0 " + n)
        lines.append(":: " + n)
    lines.append(END)
    return "\n".join(lines) + "\n"


def apply_block(hosts_text, domains):
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    stripped = pattern.sub("", hosts_text)
    if not domains:
        return stripped
    if not stripped.endswith("\n"):
        stripped += "\n"
    return stripped + block_for(domains)


def read_request(path):
    """Refuse anything but a regular file (no symlink tricks) of sane size."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("request file is not a regular file")
    if st.st_size > MAX_REQUEST_BYTES:
        raise ValueError("request file is too large")
    with open(path, encoding="utf-8") as fh:
        return valid_domains(json.load(fh).get("domains"))


def write_hosts(text):
    fd, tmp = tempfile.mkstemp(dir="/etc", prefix=".hosts.dourmouse.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.chown(tmp, 0, 0)
        os.replace(tmp, HOSTS)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def flush_dns():
    subprocess.run(["/usr/bin/dscacheutil", "-flushcache"], check=False)
    subprocess.run(["/usr/bin/killall", "-HUP", "mDNSResponder"], check=False)


def do_apply(request_file):
    try:
        domains = read_request(request_file)
    except FileNotFoundError:
        domains = []
    with open(HOSTS, encoding="utf-8") as fh:
        current = fh.read()
    updated = apply_block(current, domains)
    if updated != current:
        write_hosts(updated)
        flush_dns()
    return len(domains)


def only_root_can_write(path):
    """True when neither the path nor any directory above it can be changed
    by a non-root user (owner root, not group- or world-writable)."""
    p = os.path.abspath(path)
    while True:
        st = os.stat(p)
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        parent = os.path.dirname(p)
        if parent == p:
            return True
        p = parent


def do_install(request_file):
    if os.geteuid() != 0:
        sys.exit("run with sudo: installing the helper needs root once")
    request_file = os.path.abspath(request_file)
    os.makedirs(os.path.dirname(INSTALLED), mode=0o755, exist_ok=True)
    if not only_root_can_write(os.path.dirname(INSTALLED)):
        sys.exit("refusing to install: %s or a folder above it is writable by a non-root user"
                 % os.path.dirname(INSTALLED))
    shutil.copyfile(os.path.abspath(__file__), INSTALLED)
    os.chown(INSTALLED, 0, 0)
    os.chmod(INSTALLED, 0o755)  # noqa: S103 -- root-owned executable, must be runnable by launchd
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", INSTALLED, "--apply", "--request-file", request_file],
        "WatchPaths": [request_file],
        "RunAtLoad": True,
        "StandardErrorPath": "/var/log/dourmouse-lockdown-helper.log",
    }
    with open(PLIST, "wb") as fh:
        plistlib.dump(plist, fh)
    os.chown(PLIST, 0, 0)
    os.chmod(PLIST, 0o644)
    subprocess.run(["/bin/launchctl", "bootout", "system/" + LABEL], check=False, capture_output=True)
    subprocess.run(["/bin/launchctl", "bootstrap", "system", PLIST], check=True)
    print("Dourmouse lockdown helper installed; it watches " + request_file)


def do_uninstall():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    with open(HOSTS, encoding="utf-8") as fh:
        current = fh.read()
    cleaned = apply_block(current, [])
    if cleaned != current:
        write_hosts(cleaned)
        flush_dns()
    subprocess.run(["/bin/launchctl", "bootout", "system/" + LABEL], check=False, capture_output=True)
    for p in (PLIST, INSTALLED):
        if os.path.exists(p):
            os.unlink(p)
    print("Dourmouse lockdown helper removed and its hosts entries cleared")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dourmouse lockdown helper")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    ap.add_argument("--request-file")
    args = ap.parse_args(argv)
    if args.install:
        if not args.request_file:
            ap.error("--install needs --request-file")
        do_install(args.request_file)
    elif args.apply:
        if not args.request_file:
            ap.error("--apply needs --request-file")
        do_apply(args.request_file)
    else:
        do_uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(main())
