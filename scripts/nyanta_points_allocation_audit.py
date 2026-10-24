"""Read-only paired audit: Nyanta frozen 2026 buy counts and stake allocations.

Uses existing archived native replay legs and actual official 100-yen payouts.
Never imports or changes Nyanta's production purchase/selection logic.
Every counterfactual is paper-only, matched on the same race and same budget.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

UNIT = 100
ALTS = ("original", "uniform", "inverse_pred_odds", "probability", "retain_top_75pct_stakes", "retain_top_50pct_stakes")


def decrypt(encrypted: Path, target: Path) -> None:
    secret = os.environ.get("COLLECTOR_PASSPHRASE", "")
    if len(secret) < 32:
        raise RuntimeError("existing_archived_passphrase_unavailable")
    out = subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase-fd", "0", "--no-symkey-cache", "--output", str(target),
         "--decrypt", str(encrypted)],
        input=secret.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        check=False,
    )
    if out.returncode:
        raise RuntimeError(f"decrypt_failed_for_{encrypted.name}")


def distribute(total: int, weights: list[float]) -> list[int]:
    """Integer 100-yen, >=1 unit each, exactly preserves original total."""
    if total % UNIT or total < len(weights) * UNIT or not weights:
        raise ValueError("unallocatable_100_yen_ticket_set")
    if any(not math.isfinite(w) or w <= 0 for w in weights):
        raise ValueError("nonpositive_allocation_weight")
    units = total // UNIT
    extra = units - len(weights)
    ws = sum(weights)
    raw = [extra * w / ws for w in weights]
    whole = [int(math.floor(t)) for t in raw]
    leftovers = extra - sum(whole)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i]-whole[i]), i))
    for i in order[:leftovers]:
        whole[i] += 1
    stakes = [(v+1)*UNIT for v in whole]
    if sum(stakes) != total:
        raise AssertionError("budget_invariant_violation")
    return stakes


def normalize_combo(bt: str, combo: str) -> str:
    cars = [int(x) for x in combo.replace("=", "-").split("-")]
    if len(cars) != 3 or len(set(cars)) != 3:
        raise ValueError("invalid_combo")
    if bt == "trio":
        cars.sort()
    return "-".join(str(x) for x in cars)


def stat(count=0, stake=0, returned=0, hits=0):
    return {"n":count, "stake":stake, "return":returned, "hits":hits}


def summarize(raw: dict) -> dict:
    n, amount, ret = raw["n"], raw["stake"], raw["return"]
    return {
        **raw,
        "roi_pct": round(100*ret/amount, 3) if amount else None,
        "hit_pct": round(100*raw["hits"]/n, 3) if n else None,
        "mean_stake_per_race": round(amount/n, 2) if n else None,
    }


def sum_rows(rows: list[dict]) -> dict:
    n = len(rows)
    return summarize(stat(
        n,
        sum(r["stake"] for r in rows),
        sum(r["return"] for r in rows),
        sum(r["hit"] for r in rows),
    ))


def day_bootstrap(rows: list[dict], baseline="original", samples=750, seed=42):
    days = defaultdict(lambda: {k: [0,0] for k in ALTS})
    for x in rows:
        for a in ALTS:
            days[x["day"]][a][0] += x["stake"]
            days[x["day"]][a][1] += x["returns"][a]
    keys = sorted(days)
    if len(keys) < 10:
        return {}
    values = [days[d] for d in keys]
    rng = random.Random(seed)
    result = {}
    for a in ALTS:
        if a == baseline:
            continue
        point = 100 * (
            sum(v[a][1] for v in values)/sum(v[a][0] for v in values)
            - sum(v[baseline][1] for v in values)/sum(v[baseline][0] for v in values))
        boot = []
        for _ in range(samples):
            picked = [values[rng.randrange(len(values))] for __ in values]
            aa = sum(v[a][1] for v in picked)
            bb = sum(v[baseline][1] for v in picked)
            st_a = sum(v[a][0] for v in picked)
            st_b = sum(v[baseline][0] for v in picked)
            boot.append(100*(aa/st_a - bb/st_b))
        boot.sort()
        result[a] = {
            "delta_roi_percentage_points":round(point, 3),
            "day_cluster_bootstrap_95pct_interval_pp":[
                round(boot[int(samples*0.025)],3),
                round(boot[min(samples-1,int(samples*0.975))],3)
            ],
            "days":len(values),
            "retrospective_comparison_only":True,
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    diagnostics = Counter()
    observed = []
    paired = []
    per_month = {}
    for folder in sorted(args.inputs.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("2026-"):
            continue
        month = folder.name
        with tempfile.TemporaryDirectory(prefix="nyanta-stakes-") as temp:
            scratch = Path(temp)
            rr = scratch / "runtime.json"
            pr = scratch / "payoffs.sqlite"
            decrypt(folder/"runtime-rows.json.gpg", rr)
            decrypt(folder/f"official-payoffs-{month}.sqlite.gpg", pr)
            rows = json.loads(rr.read_text())["accepted"]
            summary = json.loads((folder/"runtime-summary.json").read_text())
            with sqlite3.connect(f"file:{pr}?mode=ro", uri=True) as db:
                pays = defaultdict(dict)
                for rk, bt, combination, payoff in db.execute(
                    "SELECT race_key,bet_type,combination,payoff FROM wt_payoffs"
                ):
                    pays[str(rk)][(str(bt),str(combination))] = int(payoff)
            if len(rows) != summary["adopted_races"]:
                raise RuntimeError(f"native_adopted_rows_mismatch_{month}")
            month_obs = []
            seen_races = set()
            for r in rows:
                rk = str(r["race_key"])
                if rk in seen_races:
                    raise RuntimeError("multiple_adoptions_in_one_race")
                seen_races.add(rk)
                legs = r.get("legs", [])
                if isinstance(legs, str):
                    legs = json.loads(legs)
                if not legs:
                    raise RuntimeError("adopted_but_empty_legs")
                bt = str(r["bet_type"])
                if bt not in ("trio","trifecta"):
                    raise RuntimeError(f"unsupported_bet_type_{bt}")
                keys = [normalize_combo(bt,str(l["combo"])) for l in legs]
                stakes = [int(l["stake"]) for l in legs]
                if any(v <= 0 or v % UNIT for v in stakes) or len(set(keys)) != len(keys):
                    raise RuntimeError(f"invalid_original_stakes_or_duplicate_{rk}")
                total = sum(stakes)
                if total > 10000 or total < 100:
                    raise RuntimeError(f"original_budget_out_of_bounds_{rk}")
                if len(legs) != int(r["n_legs"]):
                    raise RuntimeError(f"original_n_legs_mismatch_{rk}")
                s = r["settlement"]
                if s.get("settled_at") is None:
                    diagnostics["pending_rows"] += 1
                    continue
                pnl = int(s.get("payout") or 0)+int(s.get("void_refund") or 0)
                month_obs.append({"stake":total,"return":pnl,"hit":int(bool(s.get("hit")))})
                base = {
                    "race_key":rk, "day":str(r["race_date"]),
                    "n_car":int(r["n_entries"]), "type":str(r["type_label"]),
                    "plan":str(r["plan_key"]), "n_points":len(keys),
                    "bet_type":bt, "stake":total, "return":pnl,
                    "hit":int(bool(s.get("hit"))),
                    "max_stake_share":max(stakes)/total,
                    "effective_point_count":round(total*total/sum(v*v for v in stakes),3),
                }
                observed.append(base)
                if int(s.get("void_refund") or 0) > 0:
                    diagnostics["void_refund_excluded_from_paired"] += 1
                    continue
                pm = pays.get(rk)
                if not pm or not any(b==bt for b,_ in pm):
                    diagnostics["official_payoff_market_missing_for_paired"] += 1
                    continue
                payouts = [pm.get((bt,k),0) for k in keys]
                assert all(v>=0 for v in payouts)
                expected = sum(stakes[i]*payouts[i]//UNIT for i in range(len(keys)))
                if expected != int(s.get("payout") or 0):
                    diagnostics["official_payout_reconciliation_mismatch"] += 1
                    continue
                priced = [float(l.get("pred_odds") or 0) for l in legs]
                probs = [float(l.get("prob") or 0) for l in legs]
                if any(not math.isfinite(x) or x<=0 for x in priced):
                    diagnostics["invalid_predicted_odds_for_paired"] += 1
                    continue
                candidates = {
                    "original":stakes,
                    "uniform":distribute(total,[1]*len(keys)),
                    "inverse_pred_odds":distribute(total,[1/x for x in priced]),
                }
                if all(math.isfinite(x) and x>0 for x in probs):
                    candidates["probability"] = distribute(total,probs)
                else:
                    diagnostics["missing_probability_for_paired"] += 1
                    continue
                for fraction,label in ((0.75,"retain_top_75pct_stakes"),(0.5,"retain_top_50pct_stakes")):
                    keep = max(1, math.ceil(fraction*len(keys)))
                    order = sorted(range(len(keys)),key=lambda i:(-stakes[i],i))
                    subset = set(order[:keep])
                    restakes = distribute(total,[stakes[i] for i in order[:keep]])
                    alloc = [0]*len(keys)
                    for j,i in enumerate(order[:keep]):
                        alloc[i] = restakes[j]
                    candidates[label] = alloc
                counterfactual = {
                    name:sum(v * payouts[i]//UNIT for i,v in enumerate(alloc))
                    for name,alloc in candidates.items()
                }
                if counterfactual["original"] != expected:
                    raise RuntimeError("baseline_not_reconciled")
                paired.append({
                    **base, "returns":counterfactual,
                    "positive_win_leg_count":sum(p>0 for p in payouts),
                })
            saved = sum_rows(month_obs)
            if (saved["n"] != summary["settled_adopted_races"]
                or saved["stake"] != summary["stake_settled"]
                or saved["return"] != summary["official_return_settled"]):
                raise RuntimeError(f"monthly_native_settlement_totals_mismatch_{month}")
            per_month[month]=saved
            print("NYANTA_STAKE_MONTH="+json.dumps({"month":month,**saved},ensure_ascii=False),flush=True)

    if len(per_month) != 10:
        raise RuntimeError(f"require_all_ten_months_got_{sorted(per_month)}")
    if not observed or not paired:
        raise RuntimeError("actual_real_odds_payout_pairs_absent")

    def group(rows,key):
        sets=defaultdict(list)
        for r in rows:
            sets[str(key(r))].append(r)
        return {k:sum_rows(v) for k,v in sorted(sets.items())}

    scenarios={}
    for name in ALTS:
        view=[{"stake":x["stake"],"return":x["returns"][name],
               "hit":int(x["returns"][name]>0)} for x in paired]
        scenarios[name]=sum_rows(view)

    def alt_by_group(key):
        grouped=defaultdict(list)
        for x in paired:
            grouped[str(key(x))].append(x)
        return {
            name:{
                "observations":len(rows),
                "original_roi_pct":sum_rows([{"stake":r["stake"],"return":r["returns"]["original"],"hit":r["hit"]} for r in rows])["roi_pct"],
                "uniform_roi_pct":sum_rows([{"stake":r["stake"],"return":r["returns"]["uniform"],"hit":r["hit"]} for r in rows])["roi_pct"],
                "dutch_roi_pct":sum_rows([{"stake":r["stake"],"return":r["returns"]["inverse_pred_odds"],"hit":r["hit"]} for r in rows])["roi_pct"],
                "retain_75_roi_pct":sum_rows([{"stake":r["stake"],"return":r["returns"]["retain_top_75pct_stakes"],"hit":r["hit"]} for r in rows])["roi_pct"],
                "retain_50_roi_pct":sum_rows([{"stake":r["stake"],"return":r["returns"]["retain_top_50pct_stakes"],"hit":r["hit"]} for r in rows])["roi_pct"],
            } for name,rows in sorted(grouped.items())
        }

    buckets=group(observed,lambda r:r["n_points"])
    concentration=group(observed,lambda r:
        "<=25%" if r["max_stake_share"]<=0.25 else
        "<=50%" if r["max_stake_share"]<=0.50 else
        "<=75%" if r["max_stake_share"]<=0.75 else ">75%")

    report={
        "analysis":"Original native purchase selection and stakes, 2026 retrospective, plus same-race paper-only counterfactuals",
        "original_core_and_odds_models_unchanged":True,
        "original_purchase_rules_unchanged":True,
        "no_submission_or_model_retraining":True,
        "original_asof_verified":False,
        "2026_influenced_purchase_rules":True,
        "causal_interpretation_warning":"Across-point-count ROI comparisons confound race and plan difficulty. Paired stake-only alternatives fix race, ticket combos and spend but are retrospective simulation, not genuine prospective performance.",
        "months":per_month,
        "observed_settled":sum_rows(observed),
        "observed_by_n_car":group(observed,lambda r:r["n_car"]),
        "observed_by_type":group(observed,lambda r:r["type"]),
        "observed_by_plan":group(observed,lambda r:r["plan"]),
        "observed_by_bet_type":group(observed,lambda r:r["bet_type"]),
        "observed_by_point_count":buckets,
        "observed_by_max_stake_share":concentration,
        "observed_effective_point_count_mean":round(statistics.mean(r["effective_point_count"] for r in observed),4),
        "paired_n":len(paired),
        "paired_scenarios":scenarios,
        "paired_by_type":alt_by_group(lambda r:r["type"]),
        "paired_by_n_car":alt_by_group(lambda r:r["n_car"]),
        "paired_by_plan":alt_by_group(lambda r:r["plan"]),
        "paired_day_bootstrap":day_bootstrap(paired),
        "diagnostics":dict(diagnostics),
    }
    args.output.parent.mkdir(exist_ok=True,parents=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print("NYANTA_STAKE_AUDIT="+json.dumps({
        "observed_settled":report["observed_settled"],
        "point_distribution":report["observed_by_point_count"],
        "observed_by_type":report["observed_by_type"],
        "paired_scenarios":report["paired_scenarios"],
        "paired_day_bootstrap":report["paired_day_bootstrap"],
        "diagnostics":report["diagnostics"],
    },ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
