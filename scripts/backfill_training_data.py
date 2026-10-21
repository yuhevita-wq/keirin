#!/usr/bin/env python3
"""Incremental fresh-source historical collection. Never imports legacy archives."""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "backfill_progress.json"
ENCRYPTED = ROOT / "encrypted"
REPORTS = ROOT / "backfill_reports"
HISTORY_START = date(2023, 1, 1)


def read_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"schema_version": 1, "next_date": HISTORY_START.isoformat(),
            "days_attempted": 0, "days_archived": 0, "days_failed": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="", help="Manual retry YYYY-MM-DD; does not move cursor")
    parser.add_argument("--days", type=int, default=7, help="1-14 completed dates per run")
    parser.add_argument("--source", required=True, choices=["winticket", "netkeirin"])
    args = parser.parse_args()
    if not 1 <= args.days <= 14:
        raise SystemExit("days must be 1-14")
    secret = os.environ.get("COLLECTOR_PASSPHRASE", "")
    if len(secret) < 32:
        raise SystemExit("COLLECTOR_PASSPHRASE must be 32+ characters")
    state = read_state()
    cursor = date.fromisoformat(state["next_date"])
    start = date.fromisoformat(args.start_date) if args.start_date else cursor
    if start < HISTORY_START:
        raise SystemExit("Start date must be 2023-01-01 or later")
    today = date.today()
    # Runner sets TZ=Asia/Tokyo.
    last_completed = today - timedelta(days=1)
    if start > last_completed:
        print("Historical backfill caught up to yesterday; nothing to fetch.")
        return
    dates = [start + timedelta(days=i) for i in range(args.days)
             if start + timedelta(days=i) <= last_completed]
    db_rel = "data/keirin.db" if args.source == "winticket" else "db/keirin.db"
    db_path = ROOT / db_rel
    ENCRYPTED.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    outcomes = []
    for day in dates:
        day_str = day.isoformat()
        print(f"BACKFILL_DATE {day_str} {args.source}", flush=True)
        for ending in ("", "-wal", "-shm"):
            (ROOT / (db_rel + ending)).unlink(missing_ok=True)
        (ROOT / "collection_report.json").unlink(missing_ok=True)
        cmd = [sys.executable, "scripts/collect_training_day.py", "--date", day_str]
        if args.source == "netkeirin":
            cmd += ["--max-races", "0"]
        try:
            result = subprocess.run(cmd, cwd=ROOT, check=False)
            report_file = ROOT / "collection_report.json"
            report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.exists() else {}
            races = int(report.get("races", 0))
            entries = int(report.get("entries", 0))
            results = int(report.get("results", 0))
            usable = races > 0 and entries > 0 and results > 0 and db_path.is_file() and db_path.stat().st_size > 0
            status = "failed"
            if usable:
                dest = ENCRYPTED / f"{args.source}-{day_str}.sqlite.gpg"
                gpg = subprocess.run(
                    ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
                     "--passphrase-fd", "0", "--symmetric", "--cipher-algo", "AES256",
                     "--output", str(dest), str(db_path)],
                    input=secret.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False
                )
                if gpg.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
                    status = "complete" if report.get("complete") and result.returncode == 0 else "partial"
                else:
                    dest.unlink(missing_ok=True)
                    report["encryption_error"] = "encryption_failed"
            else:
                report["collection_error"] = "no_usable_race_rows"
            outcome = {"date": day_str, "source": args.source, "status": status,
                       "races": races, "entries": entries, "results": results,
                       "errors": report.get("errors", 0),
                       "exit_code": result.returncode}
        except Exception as exc:
            outcome = {"date": day_str, "source": args.source, "status": "failed",
                       "reason": type(exc).__name__}
        finally:
            for ending in ("", "-wal", "-shm"):
                (ROOT / (db_rel + ending)).unlink(missing_ok=True)
        outcomes.append(outcome)
        (REPORTS / f"{args.source}-{day_str}.json").write_text(
            json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(outcome, ensure_ascii=False), flush=True)

    summary = {"source": args.source, "start": dates[0].isoformat(),
               "end": dates[-1].isoformat(), "days": outcomes}
    (ROOT / "backfill_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Explicit manual re-collections do not move the scheduled cursor.
    if not args.start_date:
        state["next_date"] = (dates[-1] + timedelta(days=1)).isoformat()
        state["days_attempted"] = int(state.get("days_attempted", 0)) + len(outcomes)
        state["days_archived"] = int(state.get("days_archived", 0)) + sum(x["status"] != "failed" for x in outcomes)
        state["days_failed"] = int(state.get("days_failed", 0)) + sum(x["status"] == "failed" for x in outcomes)
        state["last_batch"] = {"from": dates[0].isoformat(), "to": dates[-1].isoformat(),
                               "archived": sum(x["status"] != "failed" for x in outcomes),
                               "failed": [x["date"] for x in outcomes if x["status"] == "failed"]}
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
