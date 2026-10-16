#!/usr/bin/env python3
"""Keep previously published advance dates unchanged during dashboard updates."""

import datetime as dt
import json
import os
import re
import shutil
import sys


def _copy_date(source_dir, target_dir):
    if os.path.abspath(source_dir) == os.path.abspath(target_dir):
        return
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)


def preserve_published_advance(built_data_dir, published_data_dir, collector_root):
    today = dt.datetime.now(
        dt.timezone(dt.timedelta(hours=5, minutes=30))
    ).strftime("%Y%m%d")
    built_advance = os.path.join(built_data_dir, "advance")
    published_advance = os.path.join(published_data_dir, "advance")
    os.makedirs(built_advance, exist_ok=True)

    roots = [
        published_advance,
        built_advance,
        os.path.join(collector_root, "advance", "frozen_snapshots"),
        os.path.join(collector_root, "advance", "latest_snapshots"),
    ]
    dates = set()
    for root in roots:
        if os.path.isdir(root):
            dates.update(
                name for name in os.listdir(root) if re.fullmatch(r"\d{8}", name)
            )

    for date in sorted(dates):
        frozen_dir = os.path.join(
            collector_root, "advance", "frozen_snapshots", date
        )
        latest_dir = os.path.join(
            collector_root, "advance", "latest_snapshots", date
        )
        published_dir = os.path.join(published_advance, date)
        built_dir = os.path.join(built_advance, date)

        if os.path.isdir(frozen_dir):
            source_dir = frozen_dir
        elif os.path.isdir(latest_dir):
            source_dir = latest_dir
        elif os.path.isdir(published_dir):
            if date < today:
                os.makedirs(os.path.dirname(frozen_dir), exist_ok=True)
                shutil.copytree(published_dir, frozen_dir)
                source_dir = frozen_dir
            else:
                os.makedirs(os.path.dirname(latest_dir), exist_ok=True)
                shutil.copytree(published_dir, latest_dir)
                source_dir = latest_dir
        elif os.path.isdir(built_dir):
            source_dir = built_dir
        else:
            continue

        _copy_date(source_dir, built_dir)

    manifest_path = os.path.join(built_data_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    advance_dates = []
    movie_dates = {}
    for date in sorted(os.listdir(built_advance)):
        date_dir = os.path.join(built_advance, date)
        national_path = os.path.join(date_dir, "national.json")
        if not re.fullmatch(r"\d{8}", date) or not os.path.isfile(national_path):
            continue
        advance_dates.append(date)
        with open(national_path, encoding="utf-8") as f:
            national = json.load(f)
        for movie in national.get("movies", []):
            slug = movie.get("slug")
            if slug:
                movie_dates.setdefault(slug, []).append(date)

    manifest.setdefault("modes", {}).setdefault("advance", {})["dates"] = advance_dates
    manifest.setdefault("movieDates", {})["advance"] = movie_dates
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: python preserve_advance_data.py BUILT_DATA_DIR "
            "PUBLISHED_DATA_DIR COLLECTOR_ROOT"
        )
    preserve_published_advance(sys.argv[1], sys.argv[2], sys.argv[3])
