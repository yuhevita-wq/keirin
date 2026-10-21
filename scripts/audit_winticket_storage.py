#!/usr/bin/env python3
"""Check real GitHub Release assets for completed fresh WINTICKET collection days.

Only public aggregate metadata is written. No decryption secret or race records.
"""
import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = date(2023, 1, 1)
STATE = ROOT / "state/fast_backfill_progress.json"
REPORT = ROOT / "reports/winticket_storage_audit.json"


def checked_release_assets(repo, month):
    tag = f"training-data-{month}"
    process = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if process.returncode:
        return {}, f"release_lookup_failed:{tag}"
    record = json.loads(process.stdout)
    return {
        asset.get("name", ""): int(asset.get("size") or 0)
        for asset in record.get("assets", [])
    }, None


def main():
    if not STATE.exists():
        raise SystemExit("No historical progress checkpoint found")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is required")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    last = date.fromisoformat(state["next_date"]) - timedelta(days=1)
    failed = set(state.get("failed_dates") or [])
    months = {}
    day = START
    while day <= last:
        if day.isoformat() not in failed:
            months.setdefault(day.strftime("%Y-%m"), []).append(day.isoformat())
        day += timedelta(days=1)

    report = {
        "repository": repo,
        "covered_from": START.isoformat(),
        "covered_through": last.isoformat(),
        "archived_days_in_checkpoint": state.get("days_archived"),
        "release_backups_verified": 0,
        "release_backups_missing": 0,
        "release_lookup_errors": 0,
        "months": {},
        "missing_dates_sample": [],
    }
    for month, days in sorted(months.items()):
        assets, err = checked_release_assets(repo, month)
        good, missing = 0, []
        for d in days:
            name = f"winticket-{d}.sqlite.gpg"
            if assets.get(name, 0) > 0:
                good += 1
            elif err is None:
                missing.append(d)
        report["release_backups_verified"] += good
        report["release_backups_missing"] += len(missing)
        if err:
            report["release_lookup_errors"] += 1
        report["months"][month] = {
            "expected": len(days), "verified": good,
            "missing": len(missing) if not err else None,
            "lookup_error": err,
            "release_url": f"https://github.com/{repo}/releases/tag/training-data-{month}",
        }
        report["missing_dates_sample"].extend(missing[:8])

    report["missing_dates_sample"] = report["missing_dates_sample"][:25]
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
