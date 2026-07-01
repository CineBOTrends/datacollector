"""
CI history persistence for ephemeral runners (GitHub Actions).

Each Actions run starts on an empty machine, so build_data.py would only ever
see the current date. This script keeps the collector's RAW per-date outputs in
R2 so history accumulates:

  restore        -> pull every raw <mode>/data/<date>/{finaldetailed,finalsummary}.json
                    from R2 back onto the runner  (run BEFORE build_data.py)
  save <mode>    -> push the current run's raw files for <mode> to R2
                    ("both" saves daily + advance)  (run AFTER the scrape)

R2 keys:  raw/<mode>/<YYYYMMDD>/finaldetailed.json  (+ finalsummary.json)

No-ops (prints a notice) if R2 isn't configured, so the workflow never fails.
"""
import os, sys, glob, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services.r2_storage import (
    upload_file, download_json, list_keys, is_r2_configured,
)

MODES = ("daily", "advance")
FILES = ("finaldetailed.json", "finalsummary.json")


def _rel(full_key: str) -> str:
    """Strip any R2_KEY_PREFIX so we get 'raw/<mode>/<date>/<file>'."""
    i = full_key.find("raw/")
    return full_key[i:] if i >= 0 else full_key


def restore() -> None:
    if not is_r2_configured():
        print("R2 not configured -> skipping restore (history won't accumulate)")
        return
    n = 0
    for mode in MODES:
        for key in list_keys(f"raw/{mode}/"):
            rel = _rel(key)
            parts = rel.split("/")            # raw / <mode> / <date> / <file>
            if len(parts) != 4 or parts[3] not in FILES:
                continue
            _, m, date, fname = parts
            data = download_json(rel)
            if data is None:
                continue
            outdir = os.path.join(m, "data", date)
            os.makedirs(outdir, exist_ok=True)
            with open(os.path.join(outdir, fname), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            n += 1
    print(f"restored {n} raw file(s) from R2")


def save(mode: str) -> None:
    if not is_r2_configured():
        print("R2 not configured -> skipping save")
        return
    modes = MODES if mode == "both" else (mode,)
    n = 0
    for m in modes:
        for fname in FILES:
            for local in glob.glob(os.path.join(m, "data", "*", fname)):
                date = os.path.basename(os.path.dirname(local))
                if upload_file(local, f"raw/{m}/{date}/{fname}"):
                    n += 1
    print(f"saved {n} raw file(s) to R2 (mode={mode})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "restore":
        restore()
    elif cmd == "save":
        save(sys.argv[2] if len(sys.argv) > 2 else "both")
    else:
        print("usage: ci_persist.py restore | save <daily|advance|both>")
        sys.exit(1)
