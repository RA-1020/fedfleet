"""Consistent copy of the SuperLink's state database, for the cold standby (task 2.10a).

    python3 deploy/backup/state_snapshot.py            (run every 5 min by fedfleet-state-snapshot.timer)

The SuperLink keeps its state in one SQLite file (~/fedfleet-state/state.db, WAL mode): the
registered sites' public keys, the runs and their app bundles. Copying that file with cp while the
SuperLink writes to it can give a torn copy, so this program uses SQLite's online backup API on a
READ-ONLY connection: it never writes to the live database, never stops the SuperLink, and the copy
includes every committed transaction, also those still in the WAL file.

The copy is checked (PRAGMA integrity_check) and written to ~/fedfleet-state-backup/state.db only
if its content changed since the last snapshot (sha256), so an idle aggregator makes no new file
and the Blob backup (blob_backup.py) uploads nothing. Next to it, state_snapshot.json records when
the copy was taken, its size, hash and integrity result; the exporter turns that into the gauge
fedfleet_state_snapshot_age_seconds. Both files are replaced atomically (os.replace).

Note: when nothing changes, `taken` (the snapshot time) stays old on purpose: it is the time of the
newest content, which is the recovery point. `checked` is the time of the last successful check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def copy_db(src: Path, dst: Path, timeout: float = 10.0) -> None:
    """Online backup of src into dst (a new file), reading src through a read-only connection."""
    live = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=timeout)
    try:
        out = sqlite3.connect(dst)
        try:
            live.backup(out)                     # all pages in one step, under one read transaction
            out.execute("PRAGMA journal_mode=DELETE")   # a single self-contained file, no -wal
            out.commit()
        finally:
            out.close()
    finally:
        live.close()


def integrity(path: Path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()


def snapshot(src: Path, out_dir: Path, now: float | None = None) -> dict:
    """Take one snapshot. Returns the metadata written (changed: whether state.db was replaced)."""
    now = time.time() if now is None else now
    if not src.exists():
        raise FileNotFoundError(f"{src} not found (is the SuperLink using --database there?)")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path, db_path = out_dir / "state_snapshot.json", out_dir / "state.db"
    try:
        old = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        old = {}
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".db", dir=out_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.unlink()                              # sqlite3 creates the file itself
        copy_db(src, tmp)
        check = integrity(tmp)
        if check != "ok":
            raise RuntimeError(f"integrity_check of the copy returned {check!r}; previous snapshot kept")
        digest = sha256(tmp)
        changed = digest != old.get("sha256") or not db_path.exists()
        if changed:
            os.chmod(tmp, 0o600)
            os.replace(tmp, db_path)
            taken = now
        else:
            taken = float(old.get("taken", now))
        meta = {"taken": round(taken, 3), "checked": round(now, 3), "sha256": digest,
                "size": db_path.stat().st_size, "integrity": check, "source": str(src)}
        mtmp = meta_path.with_suffix(".tmp")
        mtmp.write_text(json.dumps(meta, indent=1) + "\n")
        os.replace(mtmp, meta_path)
        return {**meta, "changed": changed}
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="~/fedfleet-state/state.db")
    p.add_argument("--out", default="~/fedfleet-state-backup")
    a = p.parse_args()
    try:
        m = snapshot(Path(a.db).expanduser(), Path(a.out).expanduser())
    except (OSError, sqlite3.Error, RuntimeError) as e:
        print(f"state snapshot failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"state snapshot {'written' if m['changed'] else 'unchanged'}: {m['size']:,} B, "
          f"sha256 {m['sha256'][:12]}, integrity {m['integrity']}")


if __name__ == "__main__":
    main()
