from contextlib import redirect_stdout
import gzip
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np

from tools import evaluate_october_eta as evaluation
from tools.train_eta import fit as original_fit
from transit.eta import EtaModel


class OctoberEvaluation(unittest.TestCase):
    def test_sharded_fit_matches_the_existing_estimator_and_date_gate(self):
        inputs = []
        old_format = []
        for i, value in enumerate((9, 23, 23, 40, 52, 60, 70, 81, 100, 140, 38, 39, 84, 36, 51, 12, 78, 80, 65, 67)):
            start = 21 if i % 3 else 16
            end = start + 1
            slot = (i % 4) // 2
            sched = 75 if i % 3 else 62
            inputs.append((10, start, end, 25, slot * 2, slot, value, sched, 1))
            old_format.append((0, 0, "10", str(start), str(end), value, sched, slot * 7200, "r"))
        rows = np.array(inputs, dtype=evaluation.LABEL)
        frozen = original_fit(old_format)
        self.assertEqual(evaluation.fit(rows), frozen)
        model = EtaModel({**frozen, "metadata": {"train": ["2026-10-12"]}})
        predictions, used = evaluation.predict(rows, model)
        expected = [model.duration(str(r["route"]), str(r["from_stop"]), str(r["to_stop"]),
                                   int(r["bin"]) * 7200, int(r["scheduled"]), service_date=evaluation.DAYS[24])[0]
                    for r in rows]
        np.testing.assert_array_equal(predictions, expected)
        self.assertTrue(np.all(used >= 3))
        rows["day"] = 11
        past, fallback = evaluation.predict(rows, model)
        np.testing.assert_array_equal(past, rows["scheduled"])
        self.assertTrue(np.all(fallback == 0))

    def test_metrics_match_numpy_and_count_equal_errors(self):
        actual = np.array([[4, -8, 0, 12], [4, -2, 3, 8], [4, 1, -3, 12]])
        book = evaluation.Metrics()
        book.add(actual[:, :2])
        book.add(actual[:, 2:])
        result = book.report()
        self.assertEqual([result[k] for k in ("candidate_closer", "candidate_further", "same_absolute_error")], [1, 1, 2])
        for i, name in enumerate(evaluation.MODELS):
            metrics = result[name]
            self.assertAlmostEqual(metrics["p90_absolute_error_seconds"], np.percentile(np.abs(actual[i]), 90))
            self.assertAlmostEqual(metrics["mae_seconds"], np.abs(actual[i]).mean())
            self.assertAlmostEqual(metrics["signed_bias_seconds"], actual[i].mean())
            self.assertEqual(metrics["early"] + metrics["exact"] + metrics["late"], 4)

    def test_partition_uses_upstream_day_and_both_receipts_at_midnight(self):
        t = evaluation.SPLIT
        with TemporaryDirectory() as tmp, patch.object(evaluation, "SHARDS", 2):
            directory = Path(tmp)
            for day in evaluation.DAYS:
                # Two October 24 labels, one still exposed to late reports, and three that end on October 25.
                records = []
                if day.day == 24:
                    records.extend([(4, 5, 6, t - 130, t - 120, t - 129, t - 119, 10, 30),
                                    (4, 5, 6, t - 30, t - 20, t - 29, t - 19, 10, 30)])
                if day.day == 25:
                    records.extend([(4, 5, 6, t - 6, t + 4, t - 5, t + 5, 10, 30),
                                    (4, 5, 6, t, t + 10, t + 1, t + 11, 10, 30),
                                    (4, 5, 6, t + 30, t + 40, t + 31, t + 41, 10, 30)])
                matrix = np.asarray(records, dtype="i4").reshape(-1, len(evaluation.COLUMNS))
                np.savez(directory / (str(day) + ".npz"), **dict(zip(evaluation.COLUMNS, matrix.T)))
                info = {"method": "delhi-directed-traversals-v1", "day": str(day), "raw": 10, "labels": len(records),
                        "routes_with_labels": int(bool(records)), "fresh_identified_positions": 9,
                        "hourly_raw": {"00": 10}, "source_member_crc32": "ABCDEF01"}
                (directory / (str(day) + ".json")).write_text(json.dumps(info), encoding="utf-8")
                with gzip.open(directory / (str(day) + ".json.gz"), "wt", encoding="utf-8") as stream:
                    json.dump({"day": str(day), "complete": True, "archive_member": {"crc32": "ABCDEF01"},
                               "census": {"by_hour": {"00": 10}, "raw_records": 10}}, stream)
            with redirect_stdout(io.StringIO()):
                _, counts, daily = evaluation.label_shards(directory, directory, directory)
            self.assertEqual((counts["training"], counts["holdout"], counts["forward"], counts["pre_split_but_received_later"]),
                             (1, 2, 5, 1))
            self.assertEqual(counts["pre_split_received_but_not_mature"], 1)
            rows = np.fromfile(directory / "0.bin", dtype=evaluation.LABEL)
            self.assertEqual(list(rows["day"]), [24, 24, 24, 25, 25])
            self.assertEqual(list(rows["flags"]), [3, 2, 2, 6, 6])
            self.assertEqual(daily[str(evaluation.DAYS[23])]["forward_fit"], 3)
            timetable = directory / "timetable.zip"
            with ZipFile(timetable, "w") as archive:
                archive.writestr("routes.txt", "route_id,route_short_name\n4,104UP\n")
            evaluation.write_json(directory / "protocol.json", {
                "columns": evaluation.COLUMNS, "method": "delhi-directed-traversals-v1",
                "official_gtfs_sha256": evaluation.sha(timetable), "matching_code_sha256": "test-only",
                "reference_trainer_sha256": evaluation.sha(evaluation.ROOT / "tools/train_eta.py"),
                "numeric_sqlite_reader_sha256": evaluation.sha(evaluation.ROOT / "tools/fleet_gps.py")})
            for folder in (directory, directory / "weekend"):
                folder.mkdir(exist_ok=True)
                with gzip.open(folder / "eta_model.json.gz", "wt") as file:
                    json.dump({"pairs": {}, "routes": {}, "metadata": {"train": ["2026-10-11"]}}, file)
            with redirect_stdout(io.StringIO()):
                evaluation.run(SimpleNamespace(gtfs=timetable, baseline=directory, labels=directory, census=directory),
                               directory, directory / "results")
            report = json.loads((directory / "results/summary.json").read_bytes())
            self.assertEqual((report["overall"]["n"], report["overall"]["candidate_closer"]), (2, 2))
            self.assertEqual(report["overall"]["candidate"]["mae_seconds"], 15)
            self.assertEqual(report["overall"]["existing"]["mae_seconds"], 20)
            for profile, expected in (("fit-01-24", 1), ("november-forward", 5)):
                with gzip.open(directory / "results" / profile / "weekend/eta_model.json.gz", "rt") as file:
                    model = json.load(file)
                self.assertEqual(model["pairs"]["4|5|6"][0], [expected, 10])


if __name__ == "__main__":
    unittest.main()
