import json
import os
import tempfile
import unittest
from unittest.mock import patch

import build_data
from build_data import (
    apply_advance_snapshot_overrides,
    freeze_past_advance_snapshots,
    frozen_advance_date_path,
    load_frozen_advance_date,
    stamp_india_gross,
)


class StampIndiaGrossTests(unittest.TestCase):
    def test_advance_cumulative_override_flows_into_later_dates(self):
        dates = [
            ("20261015", {"jailer-2": 80000000.0, "ranabaali": 10000000.0}),
            ("20261016", {"jailer-2": 115000000.0, "ranabaali": 22000000.0}),
            ("20261017", {"jailer-2": 288814023.87, "ranabaali": 34006286.60}),
            ("20261018", {"jailer-2": 100.25, "ranabaali": 200.50}),
        ]

        with tempfile.TemporaryDirectory() as mode_out:
            per_date = []
            for date, movies in dates:
                out_dir = os.path.join(mode_out, date)
                os.makedirs(os.path.join(out_dir, "m"))
                national_movies = []
                movie_data = {}
                for slug, gross in movies.items():
                    movie_data[slug] = {
                        "kpi": {"gross": gross, "sold": 0, "shows": 0}
                    }
                    national_movies.append(
                        {"slug": slug, "gross": gross, "sold": 0, "shows": 0}
                    )
                    with open(
                        os.path.join(out_dir, "m", slug + ".json"),
                        "w",
                        encoding="utf-8",
                    ) as f:
                        json.dump(movie_data[slug], f)
                with open(os.path.join(out_dir, "national.json"), "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "movies": national_movies,
                            "totals": {
                                "movies": len(national_movies),
                                "gross": sum(m["gross"] for m in national_movies),
                                "sold": 0,
                                "shows": 0,
                            },
                        },
                        f,
                    )
                result = {"date": date, "movies": movie_data}
                apply_advance_snapshot_overrides("advance", result, mode_out)
                per_date.append(result)

            stamp_india_gross("advance", per_date, mode_out)

            with open(
                os.path.join(mode_out, "20261015", "national.json"),
                encoding="utf-8",
            ) as f:
                oct15 = json.load(f)
            with open(
                os.path.join(mode_out, "20261016", "national.json"),
                encoding="utf-8",
            ) as f:
                oct16 = json.load(f)
            with open(
                os.path.join(mode_out, "20261017", "national.json"),
                encoding="utf-8",
            ) as f:
                oct17 = json.load(f)
            with open(
                os.path.join(mode_out, "20261018", "national.json"),
                encoding="utf-8",
            ) as f:
                oct18 = json.load(f)

            self.assertEqual(oct15["totals"]["gross"], 407498497.78)
            self.assertEqual(oct15["totals"]["movies"], 1)
            self.assertEqual([m["slug"] for m in oct15["movies"]], ["jailer-2"])
            self.assertFalse(
                os.path.exists(
                    os.path.join(mode_out, "20261015", "m", "ranabaali.json")
                )
            )
            self.assertEqual(oct16["totals"]["gross"], 260999254.82)
            totals17 = {m["slug"]: m["indiaGross"] for m in oct17["movies"]}
            totals18 = {m["slug"]: m["indiaGross"] for m in oct18["movies"]}
            self.assertEqual(totals17["jailer-2"], 906907140.96)
            self.assertEqual(totals17["ranabaali"], 84410922.11)
            self.assertEqual(totals18["jailer-2"], 906907241.21)
            self.assertEqual(totals18["ranabaali"], 84411122.61)
            self.assertEqual(oct18["totals"]["indiaGross"], 991318363.82)

    def test_past_advance_snapshot_is_frozen_and_used_for_future_cumulative(self):
        with tempfile.TemporaryDirectory() as root:
            advance_out = os.path.join(root, "data", "advance")
            snapshot_dir = os.path.join(advance_out, "20261015")
            movie_dir = os.path.join(snapshot_dir, "m")
            os.makedirs(movie_dir)
            national = {
                "date": "20261015",
                "last_updated": "2026-10-15 00:08 IST",
                "totals": {"gross": 407498497.78, "indiaGross": 407498497.78},
                "movies": [
                    {
                        "slug": "jailer-2",
                        "gross": 407498497.78,
                        "indiaGross": 407498497.78,
                    }
                ],
            }
            movie = {
                "title": "Jailer 2",
                "slug": "jailer-2",
                "kpi": {"gross": 407498497.78, "indiaGross": 407498497.78},
            }
            with open(os.path.join(snapshot_dir, "national.json"), "w", encoding="utf-8") as f:
                json.dump(national, f)
            with open(os.path.join(movie_dir, "jailer-2.json"), "w", encoding="utf-8") as f:
                json.dump(movie, f)

            with patch.object(build_data, "HERE", root), patch.object(
                build_data, "OUT", os.path.join(root, "data")
            ):
                freeze_past_advance_snapshots("20261016")

                # Later raw data is rebuilt with a different amount, but the
                # frozen published snapshot is still the source of truth.
                national["totals"]["gross"] = 10
                national["movies"][0]["gross"] = 10
                movie["kpi"]["gross"] = 10
                with open(os.path.join(snapshot_dir, "national.json"), "w", encoding="utf-8") as f:
                    json.dump(national, f)
                with open(os.path.join(movie_dir, "jailer-2.json"), "w", encoding="utf-8") as f:
                    json.dump(movie, f)

                restored_dir = os.path.join(root, "rebuilt", "20261015")
                os.makedirs(restored_dir)
                frozen_result = load_frozen_advance_date("20261015", restored_dir)
                frozen_date_dir = frozen_advance_date_path("20261015")
                with open(os.path.join(frozen_date_dir, "national.json"), encoding="utf-8") as f:
                    frozen_national = json.load(f)

                self.assertEqual(frozen_national["totals"]["gross"], 407498497.78)
                self.assertEqual(
                    frozen_result["movies"]["jailer-2"]["kpi"]["gross"],
                    407498497.78,
                )

                newer_dir = os.path.join(root, "rebuilt", "20261016")
                os.makedirs(os.path.join(newer_dir, "m"))
                with open(os.path.join(newer_dir, "m", "jailer-2.json"), "w", encoding="utf-8") as f:
                    json.dump({"kpi": {"gross": 100}}, f)
                with open(os.path.join(newer_dir, "national.json"), "w", encoding="utf-8") as f:
                    json.dump({"movies": [{"slug": "jailer-2", "gross": 100}], "totals": {}}, f)
                per_date = [
                    frozen_result,
                    {"date": "20261016", "movies": {"jailer-2": {"kpi": {"gross": 100}}}},
                ]
                stamp_india_gross("advance", per_date, os.path.join(root, "rebuilt"))

                with open(os.path.join(frozen_date_dir, "national.json"), encoding="utf-8") as f:
                    frozen_after_stamp = json.load(f)
                with open(os.path.join(newer_dir, "national.json"), encoding="utf-8") as f:
                    newer = json.load(f)
                self.assertEqual(frozen_after_stamp["totals"]["gross"], 407498497.78)
                self.assertEqual(newer["movies"][0]["indiaGross"], 407498597.78)


if __name__ == "__main__":
    unittest.main()
