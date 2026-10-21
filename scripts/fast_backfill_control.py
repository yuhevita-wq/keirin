#!/usr/bin/env python3
"""Partition fresh-source historical collection into non-overlapping GitHub Actions shards."""
import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
FAST_STATE = ROOT / "state" / "fast_backfill_progress.json"
LEGACY_STATE = ROOT / "state" / "backfill_progress.json"
HISTORY_START = date(2023, 1, 1)


def jst_yesterday():
    return datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)


def initial_state():
    return {
        "schema_version": 2, "next_date": HISTORY_START.isoformat(),
        "days_attempted": 0, "days_archived": 0, "days_failed": 0,
        "days_partial": 0, "failed_dates": [],
    }


def load_state(path=FAST_STATE):
    for candidate in (path, LEGACY_STATE):
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "next_date" not in data:
                raise ValueError(f"Invalid collection state: {candidate}")
            state = initial_state()
            state.update(data)
            state["schema_version"] = 2
            state.setdefault("failed_dates", [])
            return state
    return initial_state()


def write_state(state):
    FAST_STATE.parent.mkdir(parents=True, exist_ok=True)
    FAST_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def output_vars(**values):
    for key, value in values.items():
        print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def do_plan(args):
    state = load_state()
    write_state(state)  # Carry a complete checkpoint into the plan artifact.
    start = date.fromisoformat(args.start_date) if args.start_date else date.fromisoformat(state["next_date"])
    if start < HISTORY_START:
        raise ValueError("Start date must be >= 2023-01-01")
    last = min(jst_yesterday(), start + timedelta(days=args.shards * args.days_per_shard - 1))
    active = start <= last
    plan = {
        "start": start.isoformat(), "end": last.isoformat() if active else "",
        "active": active, "shards": args.shards,
        "days_per_shard": args.days_per_shard,
        "number_of_dates": (last - start).days + 1 if active else 0,
    }
    output = ROOT / "out" / "fast_backfill_plan.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_vars(active=str(active).lower(), batch_start=plan["start"],
                batch_end=plan["end"], days_per_shard=args.days_per_shard,
                date_count=plan["number_of_dates"])


def do_shard(args):
    start = date.fromisoformat(args.batch_start) + timedelta(days=args.index * args.days_per_shard)
    end = date.fromisoformat(args.batch_end)
    count = min(args.days_per_shard, (end - start).days + 1)
    output_vars(shard_start=start.isoformat(), shard_days=max(0, count))


def do_finalize(args):
    start, end = date.fromisoformat(args.batch_start), date.fromisoformat(args.batch_end)
    if start > end or end > jst_yesterday():
        raise ValueError("Invalid finalization range")

    # Prefer the original plan checkpoint over the post-checkout copy.
    base = None
    for f in sorted(Path(args.results_dir).rglob("fast_backfill_progress.json")):
        base = json.loads(f.read_text(encoding="utf-8"))
        break
    state = initial_state() if base is None else initial_state() | base

    current_cursor = date.fromisoformat(state["next_date"])
    if args.manual:
        # Manual overrides never change the automated historical cursor.
        advance_cursor = False
    elif current_cursor != start:
        raise ValueError(f"Checkpoint race: expected {current_cursor}, got {start}")
    else:
        advance_cursor = True

    found = {}
    for file in sorted(Path(args.results_dir).rglob("backfill_summary.json")):
        doc = json.loads(file.read_text(encoding="utf-8"))
        for row in doc.get("days", []):
            day = row.get("date")
            if day in found:
                raise ValueError(f"Overlapping shard data: {day}")
            if day:
                found[day] = row

    completed = []
    day = start
    while day <= end:
        key = day.isoformat()
        result = found.get(key, {"date": key, "status": "failed", "reason": "missing_shard_manifest"})
        if result.get("status") not in ("complete", "partial", "failed"):
            result["status"] = "failed"
        completed.append(result)
        day += timedelta(days=1)

    archived = sum(r["status"] in ("complete", "partial") for r in completed)
    partial = sum(r["status"] == "partial" for r in completed)
    failed = [r["date"] for r in completed if r["status"] == "failed"]
    failed_set = set(state.get("failed_dates", []))
    failed_set.update(failed)
    state["failed_dates"] = sorted(failed_set)
    if advance_cursor:
        state["next_date"] = (end + timedelta(days=1)).isoformat()
        state["days_attempted"] += len(completed)
        state["days_archived"] += archived
        state["days_partial"] += partial
        state["days_failed"] += len(failed)
    state["last_batch"] = {
        "from": start.isoformat(), "to": end.isoformat(),
        "attempted": len(completed), "archived": archived,
        "partial": partial, "failed": len(failed),
        "missing_days": failed, "manual": args.manual,
    }
    write_state(state)
    output_vars(archived=archived, failed=len(failed), partial=partial,
                has_next=str(end < jst_yesterday()).lower(),
                should_continue=str(advance_cursor and archived > 0 and
                                    len(failed) * 2 <= len(completed) and
                                    end < jst_yesterday()).lower())
    print(json.dumps(state["last_batch"], ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--start-date", default="")
    plan.add_argument("--shards", type=int, default=8)
    plan.add_argument("--days-per-shard", type=int, default=7)
    shard = sub.add_parser("shard")
    shard.add_argument("--batch-start", required=True)
    shard.add_argument("--batch-end", required=True)
    shard.add_argument("--index", type=int, required=True)
    shard.add_argument("--days-per-shard", type=int, default=7)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--batch-start", required=True)
    finalize.add_argument("--batch-end", required=True)
    finalize.add_argument("--results-dir", required=True)
    finalize.add_argument("--manual", action="store_true")
    args = parser.parse_args()
    if args.command == "plan":
        if not (1 <= args.shards <= 8 and 1 <= args.days_per_shard <= 14):
            parser.error("Allowed shards: 1-8; days-per-shard: 1-14")
        do_plan(args)
    elif args.command == "shard":
        if args.index < 0 or not 1 <= args.days_per_shard <= 14:
            parser.error("Invalid shard")
        do_shard(args)
    elif args.command == "finalize":
        do_finalize(args)


if __name__ == "__main__":
    main()
