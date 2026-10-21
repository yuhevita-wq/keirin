#!/usr/bin/env python3
"""Collect WINTICKET training records for exactly one completed day into isolated SQLite."""
import argparse
import json
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="YYYY-MM-DD; default yesterday JST")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    target = date.fromisoformat(args.date) if args.date else today - timedelta(days=1)
    if target < date(2022, 1, 1) or target >= today:
        raise SystemExit("Date must be 2022-01-01 through yesterday JST")
    from src.database import init_db, migrate_db, get_connection
    from src.scraper.pipeline_wt import WinticketPipeline
    init_db()
    migrate_db()
    stats = WinticketPipeline().collect_date(target.isoformat(), full_scan=True)
    with get_connection() as conn:
        races = conn.execute("SELECT COUNT(*) FROM wt_races").fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM wt_entries").fetchone()[0]
        odds = conn.execute("SELECT COUNT(*) FROM wt_odds").fetchone()[0]
        results = conn.execute(
            "SELECT COUNT(*) FROM wt_entries WHERE finish_order >= 1"
        ).fetchone()[0]
    errors = stats.get("errors", 0)
    report = {
        "source": "winticket", "date": target.isoformat(), "races": races,
        "entries": entries, "odds": odds, "results": results, "errors": errors,
        "complete": bool(races and entries and odds and results and not errors),
        "odds_timestamp": "collection time (not proven preclose 15-min odds)",
    }
    Path("collection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    report["status"] = "complete" if report["complete"] else "partial"
    Path("collection_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not (races and entries and results):
        raise SystemExit("No usable races, entries, and results; no shard will be published")
    if not report["complete"]:
        print("Partial day retained for archival; missing fields can be retried later.", file=sys.stderr)

if __name__ == "__main__":
    main()
