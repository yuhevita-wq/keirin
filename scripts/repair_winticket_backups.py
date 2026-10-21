#!/usr/bin/env python3
"""Repair durable WINTICKET Release backups from *newly collected* Actions shards.

This never reads legacy datasets or decrypts race data. It copies existing
encrypted .sqlite.gpg assets from 90-day Actions artifacts to permanent Releases.
"""
import io
import json
import os
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "reports/winticket_backup_repair.json"


def gh_json(endpoint):
    cmd = subprocess.run(["gh", "api", endpoint], capture_output=True,
                         check=False, timeout=35)
    if cmd.returncode:
        return None
    return json.loads(cmd.stdout)


def gh_zip(endpoint):
    cmd = subprocess.run(["gh", "api", endpoint], capture_output=True,
                         check=False, timeout=120)
    if cmd.returncode or not cmd.stdout.startswith(b"PK"):
        return None
    return zipfile.ZipFile(io.BytesIO(cmd.stdout))


def release_asset_names(repo, tag):
    obj = gh_json(f"repos/{repo}/releases/tags/{tag}")
    if obj is None:
        return None
    return {a.get("name") for a in obj.get("assets", [])
            if int(a.get("size") or 0) > 0}


def ensure_release(repo, tag):
    if release_asset_names(repo, tag) is not None:
        return True
    run = subprocess.run(
        ["gh", "release", "create", tag, "--title", tag + " encrypted training records",
         "--notes", "Fresh-source encrypted SQLite daily archives (no secret keys)."],
        capture_output=True, check=False, timeout=50,
    )
    # A competing task may have created the release.
    return run.returncode == 0 or release_asset_names(repo, tag) is not None


def load_missing_dates(repo, progress):
    start = date(2023, 1, 1)
    stop = date.fromisoformat(progress["next_date"])
    skipped = set(progress.get("failed_dates") or [])
    months = defaultdict(list)
    d = start
    while d < stop:
        if d.isoformat() not in skipped:
            months[d.strftime("%Y-%m")].append(d.isoformat())
        d += timedelta(days=1)
    missing = set()
    audit = {}
    for month, dates in months.items():
        existing = release_asset_names(repo, "training-data-" + month)
        audit[month] = {"expected": len(dates), "already_present": 0}
        for d in dates:
            name = f"winticket-{d}.sqlite.gpg"
            if existing is not None and name in existing:
                audit[month]["already_present"] += 1
            else:
                missing.add(name)
    return missing, audit


def archive_plan(zip_obj):
    if zip_obj is None:
        return None
    candidates = [n for n in zip_obj.namelist()
                  if n.endswith("fast_backfill_plan.json")]
    if not candidates:
        return None
    return json.loads(zip_obj.read(candidates[0]))


def relevant(plan, missing):
    if not plan or not plan.get("active", False):
        return False
    try:
        start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    except (KeyError, ValueError):
        return False
    d = start
    while d <= end:
        if f"winticket-{d.isoformat()}.sqlite.gpg" in missing:
            return True
        d += timedelta(days=1)
    return False


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    progress = json.loads((ROOT / "state/fast_backfill_progress.json").read_text())
    missing, monthly = load_missing_dates(repo, progress)
    initial = len(missing)
    result = {
        "repository": repo, "missing_before": initial,
        "restored_to_release": 0, "missing_after": initial,
        "months": monthly, "runs_examined": 0, "shards_examined": 0,
        "failed_uploads": 0, "missing_dates_after_sample": []
    }
    if missing:
        listing = gh_json(
            f"repos/{repo}/actions/workflows/high-speed-backfill.yml/runs?per_page=100")
        if listing is None:
            result["run_listing_error"] = True
        else:
            runs = listing.get("workflow_runs", [])
            for run in runs:
                if not missing:
                    break
                run_id = run["id"]
                if run.get("status") != "completed":
                    continue
                result["runs_examined"] += 1
                artifacts = gh_json(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
                if artifacts is None:
                    continue
                plan_artifacts = [a for a in artifacts.get("artifacts", [])
                                  if a.get("name") == f"fast-plan-{run_id}" and not a.get("expired")]
                if not plan_artifacts:
                    continue
                plan_archive = gh_zip(
                    f"repos/{repo}/actions/artifacts/{plan_artifacts[0]['id']}/zip")
                if not relevant(archive_plan(plan_archive), missing):
                    if plan_archive:
                        plan_archive.close()
                    continue
                if plan_archive:
                    plan_archive.close()
                shard_artifacts = [a for a in artifacts["artifacts"]
                                   if a.get("name", "").startswith(f"fast-shard-{run_id}-")
                                   and not a.get("expired")]
                for art in shard_artifacts:
                    if not missing:
                        break
                    archive = gh_zip(f"repos/{repo}/actions/artifacts/{art['id']}/zip")
                    if archive is None:
                        continue
                    result["shards_examined"] += 1
                    try:
                        for name in archive.namelist():
                            basename = Path(name).name
                            if (basename not in missing or not basename.startswith("winticket-")
                                or not basename.endswith(".sqlite.gpg")):
                                continue
                            month = basename[len("winticket-"):len("winticket-")+7]
                            tag = "training-data-" + month
                            if not ensure_release(repo, tag):
                                result["failed_uploads"] += 1
                                continue
                            with tempfile.TemporaryDirectory() as temp:
                                output = Path(temp) / basename
                                with archive.open(name) as src, output.open("wb") as dst:
                                    while True:
                                        chunk = src.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        dst.write(chunk)
                                if output.stat().st_size == 0:
                                    continue
                                done = subprocess.run(
                                    ["gh", "release", "upload", tag, str(output)],
                                    capture_output=True, check=False, timeout=80,
                                )
                                if done.returncode == 0:
                                    missing.remove(basename)
                                    result["restored_to_release"] += 1
                                elif basename in (release_asset_names(repo, tag) or set()):
                                    missing.remove(basename)
                                else:
                                    result["failed_uploads"] += 1
                    finally:
                        archive.close()
    result["missing_after"] = len(missing)
    result["missing_dates_after_sample"] = sorted(missing)[:24]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
