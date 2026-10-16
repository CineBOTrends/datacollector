import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from combiner.combine import TOTAL_SHARDS, combine_shards


class CombineShardsTests(unittest.TestCase):
    def test_missing_shard_preserves_existing_outputs(self):
        with tempfile.TemporaryDirectory() as base_dir:
            original_files = {
                "finaldetailed.json": b"existing detailed data",
                "finalsummary.json": b"existing summary data",
                "removed_duplicates.json": b"existing duplicate report",
            }
            for name, contents in original_files.items():
                with open(os.path.join(base_dir, name), "wb") as f:
                    f.write(contents)

            for shard_id in range(1, TOTAL_SHARDS):
                with open(os.path.join(base_dir, f"detailed{shard_id}.json"), "w") as f:
                    json.dump([], f)

            with patch(
                "combiner.combine.get_config",
                return_value={"date_code": "20261018", "base_dir": base_dir},
            ):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "missing shard files"):
                        combine_shards("advance", "20261018", upload_r2=False)

            for name, contents in original_files.items():
                with open(os.path.join(base_dir, name), "rb") as f:
                    self.assertEqual(f.read(), contents)

    def test_empty_shards_are_valid_and_complete(self):
        with tempfile.TemporaryDirectory() as base_dir:
            for shard_id in range(1, TOTAL_SHARDS + 1):
                with open(os.path.join(base_dir, f"detailed{shard_id}.json"), "w") as f:
                    json.dump([], f)

            with patch(
                "combiner.combine.get_config",
                return_value={"date_code": "20261018", "base_dir": base_dir},
            ):
                with redirect_stdout(io.StringIO()):
                    combine_shards("advance", "20261018", upload_r2=False)

            with open(os.path.join(base_dir, "finaldetailed.json"), encoding="utf-8") as f:
                detailed = json.load(f)
            with open(os.path.join(base_dir, "finalsummary.json"), encoding="utf-8") as f:
                summary = json.load(f)
            self.assertEqual(detailed["data"], [])
            self.assertEqual(summary["movies"], {})


if __name__ == "__main__":
    unittest.main()
