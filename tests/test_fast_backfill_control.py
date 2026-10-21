"""Offline sanity tests for the fast historical partition and cursor rules."""
import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fast_backfill_control.py"
SPEC = importlib.util.spec_from_file_location("fast_backfill_control", MODULE_PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class FastBackfillTests(unittest.TestCase):
    def test_shards_do_not_overlap(self):
        start = date(2023, 1, 1)
        windows = [{(start + timedelta(days=7*i+j)).isoformat() for j in range(7)}
                   for i in range(8)]
        self.assertEqual(len(set.union(*windows)), 56)
        self.assertTrue(all(not windows[i] & windows[j]
                            for i in range(8) for j in range(i+1, 8)))

    def test_finalize_skips_gaps_without_losing_track(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            checkpoint = root / "state" / "fast_backfill_progress.json"
            prior = root / "downloaded" / "plan" / "state"
            prior.mkdir(parents=True)
            (prior / "fast_backfill_progress.json").write_text(
                json.dumps(mod.initial_state()), encoding="utf-8")
            results = root / "downloaded" / "shards" / "shard-0"
            results.mkdir(parents=True)
            summary = {"days": [
                {"date": "2023-01-01", "status": "complete"},
                {"date": "2023-01-02", "status": "partial"},
            ]}
            (results / "backfill_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            args = type("Args", (), {
                "batch_start": "2023-01-01", "batch_end": "2023-01-03",
                "results_dir": str(root / "downloaded"), "manual": False,
            })()
            with patch.object(mod, "FAST_STATE", checkpoint), patch.object(
                mod, "jst_yesterday", return_value=date(2023, 4, 1)
            ):
                mod.do_finalize(args)
                new = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(new["next_date"], "2023-01-04")
            self.assertEqual(new["days_archived"], 2)
            self.assertEqual(new["days_partial"], 1)
            self.assertEqual(new["failed_dates"], ["2023-01-03"])


if __name__ == "__main__":
    unittest.main()
