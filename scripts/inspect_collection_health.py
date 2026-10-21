#!/usr/bin/env python3
"""Inspect last fast-backfill GitHub Actions runs without downloading private data."""
import io
import json
import os
import subprocess
import zipfile
from collections import Counter
from pathlib import Path


def api(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[:200])
    return json.loads(result.stdout.decode("utf-8"))


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    runs = api(f"repos/{repo}/actions/workflows/high-speed-backfill.yml/runs?per_page=6").get("workflow_runs", [])
    report = {"repository": repo, "workflow": "high-speed-backfill.yml", "runs": []}
    for run in runs[:4]:
        run_id = run["id"]
        jobs = api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100").get("jobs", [])
        artifacts = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100").get("artifacts", [])
        shard_artifacts = [a for a in artifacts if a.get("name", "").startswith("fast-shard-")]
        line = {
            "run_id": run_id,
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "html_url": run.get("html_url"),
            "job_states": [{"name": j.get("name"), "conclusion": j.get("conclusion"),
                            "status": j.get("status")} for j in jobs],
            "shard_artifact_count": len(shard_artifacts),
            "shard_bytes_total": sum(int(a.get("size_in_bytes", 0)) for a in shard_artifacts),
            "artifact_names": [a.get("name") for a in artifacts if a.get("name", "").startswith(("fast-plan-", "fast-shard-", "fast-backfill-state"))],
        }
        # Failed shards have small metadata-only ZIPs. Read only these small
        # artifacts; never ingest encrypted race data or leak encryption keys.
        reason_counts = Counter()
        sample_failures = []
        successful_races = 0
        archived_days = 0
        inspected = 0
        for artifact in shard_artifacts:
            if artifact.get("size_in_bytes", 0) > 750000 or inspected >= 8:
                continue
            got = subprocess.run(
                ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if got.returncode or not got.stdout.startswith(b"PK"):
                continue
            inspected += 1
            try:
                with zipfile.ZipFile(io.BytesIO(got.stdout)) as archive:
                    for member in archive.namelist():
                        if not member.endswith("backfill_summary.json"):
                            continue
                        summary = json.loads(archive.read(member))
                        for day in summary.get("days", []):
                            if day.get("status") == "failed":
                                detail = day.get("source_report", {})
                                reason = day.get("reason") or detail.get("collection_error") or ("exit_" + str(day.get("exit_code")))
                                reason_counts[reason] += 1
                                if len(sample_failures) < 4:
                                    sample_failures.append({"date": day.get("date"), "reason": reason,
                                                            "errors": detail.get("errors"),
                                                            "discovered_races": detail.get("discovered_races"),
                                                            "races": detail.get("races"),
                                                            "program_errors": (detail.get("program_errors") or [])[:3],
                                                            "missing_races": (detail.get("missing_races") or [])[:2]})
                            else:
                                archived_days += 1
                                successful_races += int(day.get("races", 0))
            except (ValueError, zipfile.BadZipFile):
                continue
        line["small_shard_artifacts_inspected"] = inspected
        line["sample_archive_days"] = archived_days
        line["sample_races"] = successful_races
        line["failure_reasons_sampled"] = dict(reason_counts)
        line["failed_days_examples"] = sample_failures
        report["runs"].append(line)
    dest = Path("reports/high_speed_status.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"repo": repo, "run_count": len(report["runs"]),
                      "latest_run": report["runs"][0] if report["runs"] else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
