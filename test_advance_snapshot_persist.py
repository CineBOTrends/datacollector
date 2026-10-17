import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import advance_snapshot_persist as persist


class _Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class _MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _Paginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, **kwargs):
        yield {"Contents": [{"Key": key} for key in self.objects]}


class _Client:
    def __init__(self):
        self.objects = {}

    def get_paginator(self, name):
        return _Paginator(self.objects)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _MissingObject()
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


class AdvanceSnapshotPersistTests(unittest.TestCase):
    def test_sync_uploads_once_and_restores_r2_as_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot_root = root / "frozen_snapshots"
            snapshot = snapshot_root / "20261015" / "national.json"
            snapshot.parent.mkdir(parents=True)
            snapshot_data = {"date": "20261015", "totals": {"gross": 407498497.78}}
            snapshot.write_text(json.dumps(snapshot_data), encoding="utf-8")

            client = _Client()
            config = (client, "bucket", lambda key: key)
            with patch.object(persist, "SNAPSHOT_ROOT", snapshot_root), patch.object(
                persist, "_r2", return_value=config
            ):
                self.assertEqual(persist.synchronize(), 1)
                key = "permanent/advance/20261015/national.json"
                self.assertIn(key, client.objects)

                snapshot.write_text('{"date":"20261015","totals":{"gross":0}}', encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                    persist.save_missing()

                restored = persist.restore()
                self.assertEqual(restored, 1)
                self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), snapshot_data)

    def test_apply_to_data_replaces_rebuilt_date_with_frozen_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot_root = root / "frozen_snapshots"
            snapshot_date = snapshot_root / "20261016"
            snapshot_movie = snapshot_date / "m" / "jailer-2.json"
            snapshot_movie.parent.mkdir(parents=True)
            (snapshot_date / "national.json").write_text(
                json.dumps({"movies": [{"slug": "jailer-2"}]}), encoding="utf-8"
            )
            snapshot_movie.write_text('{"permanent":true}', encoding="utf-8")

            data_root = root / "built"
            rebuilt_date = data_root / "advance" / "20261016"
            rebuilt_date.mkdir(parents=True)
            (rebuilt_date / "stale.json").write_text("{}", encoding="utf-8")

            with patch.object(persist, "SNAPSHOT_ROOT", snapshot_root):
                applied = persist.apply_to_data(data_root)

            self.assertEqual(applied, {"20261016": ["jailer-2"]})
            self.assertFalse((rebuilt_date / "stale.json").exists())
            self.assertEqual(
                json.loads(
                    (rebuilt_date / "m" / "jailer-2.json").read_text(encoding="utf-8")
                ),
                {"permanent": True},
            )


if __name__ == "__main__":
    unittest.main()
