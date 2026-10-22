#!/usr/bin/env python3
"""Read-only, production-model 7H1 dry-run. No odds proxies, no betting, no DB writes.

Run in the actual nyanta/keirin environment with all four trained models
and its current WINTICKET database:

    python scripts/dryrun_7h1_real.py --date 2026-10-22 --venue 小田原

This calls the unmodified scripts.build_7h1_candidates.build function, which
uses:
  lgbm_wt_eval + lgbm_wt_win + lgbm_wt_bad -> 67 favbust features
  lgbm_wt_favbust -> bust probability
  rank_7h1_build_legs + rank_7h1_daily_select -> final bets

It deliberately fails closed if the real models or database are missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL_NAMES = (
    "lgbm_wt_eval",
    "lgbm_wt_win",
    "lgbm_wt_bad",
    "lgbm_wt_favbust",
)


def preflight() -> None:
    missing = [name for name in MODEL_NAMES
               if not (ROOT / "data" / "models" / (name + ".pkl")).is_file()]
    if missing:
        raise RuntimeError(
            "実モデルが存在しないため推論を中止: " + ", ".join(missing)
            + "。旧 data/models/lgbm.pkl や選手勝率で代用しません。"
        )

    # A real DB is indispensable: this checks access rather than fabricating rows.
    from src.database import get_connection

    with get_connection() as conn:
        conn.execute("SELECT 1").fetchone()


def run(date: str, venue: str) -> dict:
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    if date < today:
        raise RuntimeError(
            f"{date} は過去日です。本番全期間モデルで遡及予想するとリークのため中止。"
            " 過去日検証には月次vintageモデルを使用してください。"
        )

    preflight()
    from scripts.build_7h1_candidates import build, load_context

    race_meta, entries_by_race = load_context(date, date)
    matching = [
        (rk, m) for rk, m in race_meta.items()
        if not venue or venue in str(m.get("venue_name") or "")
    ]
    if not matching:
        raise RuntimeError(
            f"{date} {venue} の7車レースがDBにありません。"
            " 出走表の収集状況を確認してください。"
        )
    broken = [
        {"race_key": rk, "n_entries_in_db": len(entries_by_race.get(rk, []))}
        for rk, _ in matching if len(entries_by_race.get(rk, [])) != 7
    ]
    if broken:
        raise RuntimeError(
            "7H1に必要な全7車の出走表が欠けています: "
            + json.dumps(broken, ensure_ascii=False)
        )

    # IMPORTANT: same production inference + 7H1 thresholds and construction.
    # No custom feature-engineering, imputed odds, or hand-built probabilities.
    selected = build(
        date, date,
        "lgbm_wt_eval", "lgbm_wt_win",
        "lgbm_wt_bad", "lgbm_wt_favbust",
    )
    chosen = {
        c["race_key"]: c for c in selected
        if not venue or venue in str(c.get("venue_name") or "")
    }
    rows = []
    for rk, m in sorted(matching, key=lambda pair: int(pair[1].get("race_no") or 0)):
        c = chosen.get(rk)
        if c is None:
            # build() may omit a race for numerous reasons, including invalid
            # input or missing WT marks. We cannot assert any specific gate failed.
            rows.append({
                "race_key": rk, "venue": m.get("venue_name"),
                "race_no": m.get("race_no"),
                "status": "未採用・原因未特定（候補生成器から未出力）",
            })
            continue
        rows.append({
            "race_key": rk, "venue": c.get("venue_name"),
            "race_no": c.get("race_no"),
            "status": "7H1採用",
            "fav": c.get("fav"), "fav_name": c.get("fav_name"),
            "gap12": c.get("gap12"), "bust_prob": c.get("bust_prob"),
            "legs_tf": c.get("legs_tf"), "legs_trio": c.get("legs_trio"),
            "stake_tf": c.get("stake_tf"),
            "stake_trio": c.get("stake_trio"),
            "bet_amount": c.get("bet_amount"),
        })
    return {
        "date": date, "venue_filter": venue, "method": "original_models_and_build",
        "total_7car_races": len(matching),
        "selected": sum(x["status"] == "7H1採用" for x in rows),
        "races": rows,
        "read_only": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (today or future)")
    ap.add_argument("--venue", default="", help="Substring, e.g. 小田原")
    ap.add_argument("--output", default="", help="Optional separate JSON report path")
    args = ap.parse_args()
    try:
        report = run(args.date, args.venue)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "実モデル推論未実施", "error": str(exc),
                 "models": list(MODEL_NAMES), "read_only": True},
                ensure_ascii=False, indent=2
            ), file=sys.stderr
        )
        return 2
    result = json.dumps(report, ensure_ascii=False, indent=2)
    print(result)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
