#!/usr/bin/env python3
"""Keep frozen dashboard advance snapshots backed by immutable R2 objects.

R2 keys use permanent/advance/<YYYYMMDD>/<snapshot-file>.json. They are kept
separate from v1/advance, whose objects use the collector finalsummary schema.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAPSHOT_ROOT = ROOT / "advance" / "frozen_snapshots"
DATA_PREFIX = "permanent/advance/"


def _load_local_env():
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("$env:"):
            name = name[5:]
        if name not in {
            "R2_ENDPOINT",
            "R2_ACCESS_KEY",
            "R2_SECRET_KEY",
            "R2_BUCKET_NAME",
            "R2_KEY_PREFIX",
        }:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _r2():
    _load_local_env()
    from services.r2_storage import (
        R2_BUCKET_NAME,
        _full_key,
        get_r2_client,
        is_r2_configured,
    )

    if not is_r2_configured():
        return None
    return get_r2_client(), R2_BUCKET_NAME, _full_key


def _snapshot_files():
    if not SNAPSHOT_ROOT.is_dir():
        return
    for date_dir in sorted(SNAPSHOT_ROOT.iterdir()):
        if not date_dir.is_dir() or not re.fullmatch(r"\d{8}", date_dir.name):
            continue
        for path in sorted(date_dir.rglob("*.json")):
            relative = path.relative_to(date_dir)
            if ".." in relative.parts:
                raise ValueError(f"Invalid advance snapshot path: {path}")
            with path.open("rb") as f:
                json.load(f)
            yield date_dir.name, relative, path


def restore():
    """Restore R2's immutable snapshots locally; remote copies are authoritative."""
    config = _r2()
    if config is None:
        print("R2 not configured; keeping checked-in frozen advance snapshots")
        return 0

    client, bucket, full_key = config
    prefix = full_key(DATA_PREFIX)
    paginator = client.get_paginator("list_objects_v2")
    grouped = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            relative = key[len(prefix) :]
            parts = relative.split("/")
            if (
                len(parts) < 2
                or not re.fullmatch(r"\d{8}", parts[0])
                or any(part in ("", ".", "..") for part in parts)
                or not parts[-1].endswith(".json")
            ):
                raise ValueError(f"Invalid permanent advance snapshot key: {key}")
            grouped.setdefault(parts[0], []).append((key, parts[1:]))

    restored = 0
    for date, objects in grouped.items():
        target_dir = SNAPSHOT_ROOT / date
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for key, relative_parts in objects:
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"].read()
            json.loads(body)
            destination = target_dir.joinpath(*relative_parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, delete=False
            ) as temp:
                temp.write(body)
                temp_path = temp.name
            os.replace(temp_path, destination)
            restored += 1
    print(f"Restored {restored} frozen advance snapshot file(s) from R2")
    return restored


def save_missing():
    """Store missing frozen snapshots, never overwriting an existing R2 object."""
    config = _r2()
    if config is None:
        print("R2 not configured; frozen advance snapshots remain checked in")
        return 0

    client, bucket, full_key = config
    saved = 0
    for date, relative, path in _snapshot_files() or ():
        key = full_key(f"{DATA_PREFIX}{date}/{relative.as_posix()}")
        with path.open("rb") as f:
            body = f.read()
        try:
            remote = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code not in {"NoSuchKey", "404", "NotFound"}:
                raise RuntimeError(
                    f"Could not check permanent R2 snapshot {key}: {type(error).__name__}"
                ) from error
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
            remote = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            if remote != body:
                raise RuntimeError(f"Permanent R2 snapshot verification failed: {key}")
            saved += 1
            continue
        if remote != body:
            raise RuntimeError(
                f"Refusing to overwrite different permanent R2 snapshot: {key}"
            )
    print(f"Stored and verified {saved} new frozen advance snapshot file(s) in R2")
    return saved


def synchronize():
    """Pull R2's permanent snapshots, then archive any newly frozen local dates."""
    if _r2() is None:
        print("R2 not configured; using checked-in frozen advance snapshots")
        return 0
    restored = restore()
    saved = save_missing()
    return restored + saved


def apply_to_data(data_root):
    """Replace built advance date folders with their permanent frozen copies."""
    applied = {}
    for date_dir in sorted(SNAPSHOT_ROOT.iterdir() if SNAPSHOT_ROOT.is_dir() else []):
        if not date_dir.is_dir() or not re.fullmatch(r"\d{8}", date_dir.name):
            continue
        national_path = date_dir / "national.json"
        if not national_path.is_file():
            raise ValueError(f"Frozen advance snapshot is missing {national_path}")
        with national_path.open(encoding="utf-8") as f:
            national = json.load(f)
        slugs = [movie["slug"] for movie in national.get("movies", [])]
        target = Path(data_root) / "advance" / date_dir.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(date_dir, target)
        applied[date_dir.name] = slugs
    return applied


if __name__ == "__main__":
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if command == "restore":
        restore()
    elif command == "save":
        save_missing()
    elif command == "sync":
        synchronize()
    else:
        raise SystemExit("usage: python advance_snapshot_persist.py [restore|save|sync]")
