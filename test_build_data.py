import json
import os
import tempfile
import unittest

from build_data import stamp_india_gross


class StampIndiaGrossTests(unittest.TestCase):
    def test_advance_cumulative_override_flows_into_later_dates(self):
        dates = [
            ("20261015", {"jailer-2": 407498497.78}),
            ("20261016", {"jailer-2": 210594619.31, "ranabaali": 50404635.51}),
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
                    movie_data[slug] = {"kpi": {"gross": gross}}
                    national_movies.append({"slug": slug, "gross": gross})
                    with open(
                        os.path.join(out_dir, "m", slug + ".json"),
                        "w",
                        encoding="utf-8",
                    ) as f:
                        json.dump({"kpi": {"gross": gross}}, f)
                with open(os.path.join(out_dir, "national.json"), "w", encoding="utf-8") as f:
                    json.dump(
                        {"movies": national_movies, "totals": {}},
                        f,
                    )
                per_date.append({"date": date, "movies": movie_data})

            stamp_india_gross("advance", per_date, mode_out)

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

            totals17 = {m["slug"]: m["indiaGross"] for m in oct17["movies"]}
            totals18 = {m["slug"]: m["indiaGross"] for m in oct18["movies"]}
            self.assertEqual(totals17["jailer-2"], 906907140.96)
            self.assertEqual(totals17["ranabaali"], 84410922.11)
            self.assertEqual(totals18["jailer-2"], 906907241.21)
            self.assertEqual(totals18["ranabaali"], 84411122.61)
            self.assertEqual(oct18["totals"]["indiaGross"], 991318363.82)


if __name__ == "__main__":
    unittest.main()
