"""Copy FedFleet checkpoints and metrics to Azure Blob storage (standard library only).

    python deploy/backup/blob_backup.py --sas-file ~/.fedfleet-blob-sas

Every --interval seconds it looks at each run folder (~/fedfleet-runs*, one level of
subfolders too) and uploads latest_checkpoint.pt, rounds.jsonl and final_exchanged.pt, but
only files whose size or modification time changed since their last successful upload, so an
idle aggregator makes no requests at all. The blob name is the file's path under the home
folder, e.g. fedfleet-runs-crash/latest_checkpoint.pt.

The SAS file holds one line: the container URL with a SAS token, from the Azure portal
(container > Shared access tokens, permissions Create and Write, HTTPS only). It is a secret:
keep it chmod 600 on the VM, never in git, never in logs. This program never prints it.
Checkpoints are replaced atomically by the aggregator (os.replace), so a copy is never half a
checkpoint; rounds.jsonl may be caught mid-append and is uploaded again after the next round.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FILES = ("latest_checkpoint.pt", "rounds.jsonl", "final_exchanged.pt", "model.onnx", "model_card.md")
PATTERNS = ["~/fedfleet-runs*/{f}", "~/fedfleet-runs*/*/{f}"]
API_VERSION = "2021-12-02"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def read_sas(path: str) -> tuple[str, str]:
    """Return (container URL without query, SAS query string)."""
    raw = Path(path).expanduser().read_text().strip()
    u = urllib.parse.urlsplit(raw)
    if u.scheme != "https" or not u.query or "sig=" not in u.query:
        sys.exit("SAS file must hold one https container URL with a SAS token (?sv=...&sig=...)")
    return f"https://{u.netloc}{u.path.rstrip('/')}", u.query


def candidates(home: Path) -> list[Path]:
    found = set()
    for pat in PATTERNS:
        for f in FILES:
            for p in glob.glob(os.path.expanduser(pat.format(f=f))):
                found.add(Path(p))
    return sorted(found)


def blob_name(path: Path, home: Path) -> str:
    rel = path.resolve().relative_to(home.resolve())
    return "/".join(urllib.parse.quote(part) for part in rel.parts)


def put_blob(base: str, sas: str, name: str, data: bytes, timeout: float) -> int:
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    req = urllib.request.Request(f"{base}/{name}?{sas}", data=data, method="PUT", headers={
        "x-ms-blob-type": "BlockBlob",
        "x-ms-version": API_VERSION,
        "Content-MD5": md5,
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def run_once(base: str, sas: str, state: dict, home: Path, timeout: float) -> int:
    uploaded = 0
    for path in candidates(home):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        key = str(path.resolve())
        sig = [st.st_size, st.st_mtime_ns]
        if state.get(key) == sig:
            continue
        name = blob_name(path, home)
        try:
            data = path.read_bytes()
            if os.stat(path).st_mtime_ns != st.st_mtime_ns:
                continue                       # changed while reading: next cycle
            status = put_blob(base, sas, name, data, timeout)
        except urllib.error.HTTPError as e:
            log(f"upload failed {name}: HTTP {e.code} {e.reason}")   # the URL (with SAS) is not printed
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"upload failed {name}: {type(e).__name__}")
            continue
        state[key] = sig
        uploaded += 1
        log(f"uploaded {name} ({len(data):,} B, HTTP {status})")
    return uploaded


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sas-file", default="~/.fedfleet-blob-sas")
    p.add_argument("--state", default="~/.fedfleet-blob-state.json", help="what was uploaded already")
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    a = p.parse_args()
    base, sas = read_sas(a.sas_file)
    home = Path.home()
    state_path = Path(a.state).expanduser()
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    log(f"backing up to {urllib.parse.urlsplit(base).netloc}{urllib.parse.urlsplit(base).path}, "
        f"every {a.interval:g} s, {len(state)} files already uploaded")
    while True:
        if run_once(base, sas, state, home, a.timeout):
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, state_path)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
