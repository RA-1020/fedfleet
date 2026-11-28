"""The state snapshot for the cold standby: consistent, read-only, unchanged content left alone."""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "backup"))
sys.path.insert(0, str(ROOT / "deploy" / "monitoring"))
import blob_backup  # noqa: E402
import fedfleet_exporter as ex  # noqa: E402
import state_snapshot as ss  # noqa: E402


def live_db(path: Path, rows: int = 50, keep_open: bool = False):
    """A WAL database like the SuperLink's. With keep_open the connection stays open (as the
    SuperLink's does), so the committed rows are still only in the WAL file."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")       # keep everything in the WAL
    con.execute("CREATE TABLE node (id INTEGER PRIMARY KEY, public_key BLOB UNIQUE)")
    con.executemany("INSERT INTO node (public_key) VALUES (?)", [(f"key-{i}".encode(),) for i in range(rows)])
    con.commit()
    if keep_open:
        return con
    con.close()
    return None


def count(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT count(*) FROM node").fetchone()[0]
    finally:
        con.close()


def test_snapshot_holds_wal_content_and_is_one_file(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    con = live_db(src, keep_open=True)
    assert (tmp_path / "state.db-wal").stat().st_size > 0     # the rows are only in the WAL so far
    m = ss.snapshot(src, out, now=1000.0)
    con.close()
    assert m["changed"] and m["integrity"] == "ok"
    assert count(out / "state.db") == 50
    assert not (out / "state.db-wal").exists()                # self-contained copy
    meta = json.loads((out / "state_snapshot.json").read_text())
    assert meta["taken"] == 1000.0 and meta["sha256"] == ss.sha256(out / "state.db")
    assert (out / "state.db").stat().st_mode & 0o777 == 0o600
    assert [p.name for p in out.iterdir() if p.name.startswith(".")] == []   # no temporary files left


def test_unchanged_content_keeps_file_and_recovery_point(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    live_db(src)
    ss.snapshot(src, out, now=1000.0)
    mtime = (out / "state.db").stat().st_mtime_ns
    m = ss.snapshot(src, out, now=1300.0)
    assert not m["changed"]
    assert (out / "state.db").stat().st_mtime_ns == mtime     # the Blob backup sees nothing new
    meta = json.loads((out / "state_snapshot.json").read_text())
    assert meta["taken"] == 1000.0 and meta["checked"] == 1300.0


def test_changed_content_replaces_snapshot(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    live_db(src)
    ss.snapshot(src, out, now=1000.0)
    con = sqlite3.connect(src)
    con.execute("INSERT INTO node (public_key) VALUES (?)", (b"new",))
    con.commit()
    con.close()
    m = ss.snapshot(src, out, now=1300.0)
    assert m["changed"] and m["taken"] == 1300.0 and count(out / "state.db") == 51


def test_live_database_is_not_written(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    live_db(src)
    before = src.read_bytes()
    ss.snapshot(src, out)
    assert src.read_bytes() == before


def test_snapshot_while_a_writer_is_busy(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    live_db(src, rows=0)
    stop = threading.Event()

    def writer():
        con = sqlite3.connect(src)
        i = 0
        while not stop.is_set():
            con.execute("INSERT INTO node (public_key) VALUES (?)", (f"w-{i}".encode(),))
            con.commit()
            i += 1
        con.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.05)
        results = [ss.snapshot(src, out) for _ in range(5)]
    finally:
        stop.set()
        t.join()
    assert all(r["integrity"] == "ok" for r in results)
    assert count(out / "state.db") > 0


def test_missing_database_fails_without_touching_the_old_snapshot(tmp_path):
    src, out = tmp_path / "state.db", tmp_path / "backup"
    live_db(src)
    ss.snapshot(src, out, now=1000.0)
    old = (out / "state.db").read_bytes()
    src.unlink()
    (tmp_path / "state.db-wal").unlink(missing_ok=True)
    try:
        ss.snapshot(src, out, now=1300.0)
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
    assert (out / "state.db").read_bytes() == old


def test_blob_backup_uploads_the_snapshot_not_the_live_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "fedfleet-state").mkdir()
    (tmp_path / "fedfleet-state" / "state.db").write_bytes(b"live")
    (tmp_path / "fedfleet-state-backup").mkdir()
    for f in ("state.db", "state_snapshot.json"):
        (tmp_path / "fedfleet-state-backup" / f).write_bytes(b"x")
    names = [blob_backup.blob_name(p, tmp_path) for p in blob_backup.candidates(tmp_path)]
    assert names == ["fedfleet-state-backup/state.db", "fedfleet-state-backup/state_snapshot.json"]


def test_exporter_reports_snapshot_ages(tmp_path):
    p = tmp_path / "state_snapshot.json"
    assert ex.snapshot_text(p) == ""
    p.write_text(json.dumps({"taken": 1000.0, "checked": 1600.0, "size": 4096}))
    text = ex.snapshot_text(p, now=1700.0)
    assert "fedfleet_state_snapshot_age_seconds 700.0" in text
    assert "fedfleet_state_snapshot_checked_age_seconds 100.0" in text
    assert "fedfleet_state_snapshot_bytes 4096.0" in text
