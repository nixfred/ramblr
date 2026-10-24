"""Nightly encrypted backup of the transcript DB to whi.

sqlite online-backup snapshot -> gpg public-key encryption (the key pair lives only on gus, in
~/.config/ramblr/backup-gnupg, mode 700; whi only ever receives ciphertext) -> ssh stream to
whi:~/backups/ramblr/ears-<stamp>.db.gpg. Each night is a new file: nothing is pruned, on either side.

  python -m ramblr.backup run            one backup now
  python -m ramblr.backup test-restore   fetch the newest backup into a temp dir under /tmp, decrypt, count rows,
                                         then remove only that temp copy
"""
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from . import config, db as dbm

TABLES = ("segments", "speakers", "ideas", "action_items", "summaries", "audit")


def gpg(*args, **kw):
    return subprocess.run(["gpg", "--homedir", str(config.BACKUP_GNUPG), "--batch", "--quiet", *args], **kw)


def ensure_key():
    """Create the backup key pair once (no passphrase: the nightly job is unattended; the secret key never leaves gus)."""
    config.BACKUP_GNUPG.mkdir(parents=True, exist_ok=True)
    os.chmod(config.BACKUP_GNUPG, 0o700)
    have = gpg("--list-keys", config.BACKUP_RECIPIENT, capture_output=True)
    if have.returncode == 0:
        return False
    gpg("--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key",
        f"Ramblr backup <{config.BACKUP_RECIPIENT}>", "default", "default", "never", check=True, capture_output=True)
    return True


def counts(conn):
    out = {}
    for t in TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    return out


def ssh(cmd, **kw):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", config.BACKUP_HOST, cmd], **kw)


def run(db=None):
    ensure_key()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = config.DATA / f".backup-{stamp}.db"
    old = os.umask(0o077)                    # the plaintext snapshot is never readable by anyone else, not even briefly
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(snap)
    os.umask(old)
    try:
        src.backup(dst)                      # consistent online snapshot, writers keep going
        n = counts(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(snap, 0o600)
    remote_dir = config.BACKUP_DIR
    name = f"ears-{stamp}.db.gpg"
    path = f"{remote_dir}/{name}"
    t = time.monotonic()
    try:
        enc = subprocess.Popen(["nice", "-n", "19", "gpg", "--homedir", str(config.BACKUP_GNUPG), "--batch", "--quiet",
                                "--trust-model", "always", "--encrypt", "-r", config.BACKUP_RECIPIENT, "-o", "-", str(snap)],
                               stdout=subprocess.PIPE)
        q = shlex.quote
        up = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", config.BACKUP_HOST,
                               f"umask 077 && mkdir -p {q(remote_dir)} && cat > {q(path + '.part')} && mv {q(path + '.part')} {q(path)}"],
                              stdin=subprocess.PIPE)
        h, size = hashlib.sha256(), 0
        while True:
            b = enc.stdout.read(1 << 20)
            if not b:
                break
            h.update(b)
            size += len(b)
            up.stdin.write(b)
        up.stdin.close()
        if enc.wait() != 0 or up.wait() != 0:
            raise RuntimeError(f"backup pipeline failed (gpg {enc.returncode}, ssh {up.returncode})")
    finally:
        snap.unlink(missing_ok=True)         # the plaintext snapshot this run made, never anything else
    remote = ssh(f"sha256sum {shlex.quote(path)}", capture_output=True, text=True, timeout=60).stdout.split()
    ok = bool(remote) and remote[0] == h.hexdigest()
    res = {"host": config.BACKUP_HOST, "path": f"~/{path}", "bytes": size, "sha256": h.hexdigest(), "verified": ok,
           "counts": n, "seconds": round(time.monotonic() - t, 2), "at": stamp}
    if db is None:
        db = dbm.connect(config.DB_PATH)
    if config.FAMILY_DB.exists():        # B4-14: the family archive is backed up as its own encrypted file
        res["family"] = _family_backup(stamp)
    dbm.set_meta(db, "backup_last", json.dumps(res))
    if not ok:
        raise RuntimeError(f"remote checksum mismatch for {path}")
    fam = res.get("family")
    if fam is not None and not (fam.get("ok") and fam.get("verified", True)):
        raise RuntimeError("family archive backup failed")   # the main backup is recorded above; this run still fails
    return res


def _family_backup(stamp):
    snap = config.DATA / f".backup-family-{stamp}.db"
    old = os.umask(0o077)
    src, dst = sqlite3.connect(config.FAMILY_DB, timeout=30), sqlite3.connect(snap)
    os.umask(old)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    q = shlex.quote
    path = f"{config.BACKUP_DIR}/family-{stamp}.db.gpg"
    try:
        enc = subprocess.Popen(["nice", "-n", "19", "gpg", "--homedir", str(config.BACKUP_GNUPG), "--batch", "--quiet", "--trust-model", "always",
                                "--encrypt", "-r", config.BACKUP_RECIPIENT, "-o", "-", str(snap)], stdout=subprocess.PIPE)
        up = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", config.BACKUP_HOST,
                             f"umask 077 && cat > {q(path + '.part')} && mv {q(path + '.part')} {q(path)}"], stdin=enc.stdout)
        enc.wait()
        return {"path": f"~/{path}", "ok": enc.returncode == 0 and up.returncode == 0}
    finally:
        snap.unlink(missing_ok=True)


def test_restore():
    """Newest backup from whi -> temp dir -> decrypt -> row counts vs the counts recorded when it was made."""
    db = dbm.connect(config.DB_PATH)
    last = json.loads(dbm.get_meta(db, "backup_last", "{}") or "{}")
    if not last:
        raise SystemExit("no backup recorded yet")
    tmp = tempfile.mkdtemp(prefix="ramblr-restore-", dir="/tmp")
    try:
        os.chmod(tmp, 0o700)
        enc, out = os.path.join(tmp, "b.db.gpg"), os.path.join(tmp, "restore.db")
        rel = last["path"].removeprefix("~/")
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", f"{config.BACKUP_HOST}:{rel}", enc], check=True, timeout=600)
        gpg("--pinentry-mode", "loopback", "--passphrase", "", "-o", out, "--decrypt", enc, check=True, capture_output=True)
        conn = sqlite3.connect(out)
        got = counts(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        res = {"backup": last["path"], "restored_counts": got, "counts_at_backup": last["counts"],
               "match": got == last["counts"], "integrity": integrity, "temp_dir": tmp}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # only the temp restore copy this function created
    res["temp_removed"] = not os.path.exists(tmp)
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        print(json.dumps(run(), indent=1))
    elif cmd == "test-restore":
        print(json.dumps(test_restore(), indent=1))
    else:
        print(__doc__)
        sys.exit(2)
