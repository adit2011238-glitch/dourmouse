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

The request file lives in a folder the user controls, so the helper treats it
as untrusted: it opens it without following links, reads through the open
descriptor with a size limit, requires it to be a regular file owned by the
invoking user and not writable by others, refuses names that macOS itself
depends on (Apple update, certificate and iCloud hosts), and treats a corrupt
or unsafe file as an empty request so the block is always removable.

  sudo /usr/bin/python3 lockdown_helper.py --install --request-file PATH
  sudo /usr/bin/python3 /Library/PrivilegedHelperTools/com.dourmouse.lockdown-helper --uninstall
"""

import argparse
import errno
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
MAX_DOMAINS = 500
MAX_REQUEST_BYTES = 256 * 1024
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
# Names the Mac needs to stay safe and usable. Blocking one would switch off
# certificate revocation checks, XProtect and software updates, iCloud or the
# machine's own loopback names, so the helper never writes them.
RESERVED_NAMES = frozenset({"localhost", "broadcasthost", "local", "localdomain"})
RESERVED_SUFFIXES = (
    "localhost", "local", "localdomain", "arpa", "apple.com", "icloud.com", "icloud-content.com",
    "apple-dns.net", "cdn-apple.com", "mzstatic.com", "digicert.com", "lencr.org", "letsencrypt.org",
    "sectigo.com", "globalsign.com", "entrust.net",
)
RESERVED_LABEL_RE = re.compile(r"^(ocsp|crl)[0-9]*$")


class RequestError(ValueError):
    """The request file is unsafe or unreadable; the helper treats it as empty."""


def log(message):
    print("dourmouse-lockdown-helper: " + message, file=sys.stderr)


def is_reserved(name):
    """True for a name that must never be blocked (see RESERVED_SUFFIXES)."""
    if name in RESERVED_NAMES or RESERVED_LABEL_RE.fullmatch(name.split(".")[0]):
        return True
    return any(name == suffix or name.endswith("." + suffix) for suffix in RESERVED_SUFFIXES)


def is_valid_name(name):
    """A DNS name a hosts file can block: ASCII letters, digits and hyphens,
    dot-separated labels, with a non-numeric last label so an IP address
    literal is never taken for a name."""
    return bool(DOMAIN_RE.fullmatch(name)) and not name.rsplit(".", 1)[-1].isdigit()


def valid_domains(raw):
    """Only well-formed, non-reserved lower-case domain names survive;
    anything else is dropped (and logged), never written. More than
    MAX_DOMAINS entries is refused outright rather than cut short."""
    if not isinstance(raw, list):
        return []
    if len(raw) > MAX_DOMAINS:
        raise RequestError("the request lists more than %d names" % MAX_DOMAINS)
    out = []
    for d in raw:
        name = d.lower() if isinstance(d, str) else None
        if name is None or not is_valid_name(name):
            log("dropped an invalid name from the request")
        elif is_reserved(name):
            log("refused to block %s: macOS depends on it" % name)
        else:
            out.append(name)
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


def _read_limited(fd):
    chunks, total = [], 0
    while total <= MAX_REQUEST_BYTES:
        chunk = os.read(fd, min(65536, MAX_REQUEST_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def read_request(path, owner_uid=None):
    """Open without following links, then judge the open descriptor (not the
    name, which could be swapped after a check): a regular file of sane size,
    owned by the invoking user and not writable by anyone else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise RequestError("request file is not a regular file (it is a link)") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RequestError("request file is not a regular file")
        if st.st_size > MAX_REQUEST_BYTES:
            raise RequestError("request file is too large")
        expected = owner_uid if owner_uid is not None else os.stat(os.path.dirname(os.path.abspath(path))).st_uid
        if st.st_uid != expected:
            raise RequestError("request file is not owned by the invoking user")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RequestError("request file is writable by other users")
        data = _read_limited(fd)
    finally:
        os.close(fd)
    if len(data) > MAX_REQUEST_BYTES:
        raise RequestError("request file is too large")
    try:
        request = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise RequestError("request file is not valid JSON: %s" % exc) from exc
    if not isinstance(request, dict):
        raise RequestError("request file is not a JSON object")
    return valid_domains(request.get("domains"))


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


def do_apply(request_file, owner_uid=None):
    try:
        domains = read_request(request_file, owner_uid)
    except FileNotFoundError:
        domains = []
    except (ValueError, OSError) as exc:
        # Refuse the request and clear the block: a damaged file must never
        # leave a stale block behind or stop the helper from running.
        log("refused the request file %s (%s); clearing the block" % (request_file, exc))
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
    owner_uid = int(os.environ.get("SUDO_UID") or os.stat(os.path.dirname(request_file)).st_uid)
    if owner_uid == 0:
        sys.exit("refusing to install: run with sudo from your own account so the helper knows whose file to trust")
    os.makedirs(os.path.dirname(INSTALLED), mode=0o755, exist_ok=True)
    if not only_root_can_write(os.path.dirname(INSTALLED)):
        sys.exit("refusing to install: %s or a folder above it is writable by a non-root user"
                 % os.path.dirname(INSTALLED))
    shutil.copyfile(os.path.abspath(__file__), INSTALLED)
    os.chown(INSTALLED, 0, 0)
    os.chmod(INSTALLED, 0o755)  # noqa: S103 -- root-owned executable, must be runnable by launchd
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", INSTALLED, "--apply", "--request-file", request_file,
                            "--owner-uid", str(owner_uid)],
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
    ap.add_argument("--owner-uid", type=int)
    args = ap.parse_args(argv)
    if args.install:
        if not args.request_file:
            ap.error("--install needs --request-file")
        do_install(args.request_file)
    elif args.apply:
        if not args.request_file:
            ap.error("--apply needs --request-file")
        do_apply(args.request_file, args.owner_uid)
    else:
        do_uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(main())
