#!/usr/bin/env python3
"""Offline diagnostics only; eval.scoring.compute remains the scoring protocol."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.scoring import compute


def curve_metrics(series, initial_assets):
    if not series:
        raise ValueError("missing net_assets series; refusing cash fallback for analysis")
    points = [(int(t), float(v)) for t, v in series]
    if any(not math.isfinite(v) for _, v in points):
        raise ValueError("non-finite net_assets")
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ValueError("net_assets timestamps must be strictly increasing")
    peak = float(initial_assets)
    if peak <= 0:
        raise ValueError("initial assets must be positive")
    max_abs = max_frac = 0.0
    for _, value in points:
        peak = max(peak, value)
        max_abs = max(max_abs, peak - value)
        max_frac = max(max_frac, (peak - value) / peak)
    return {"max_drawdown_absolute": max_abs, "max_drawdown_fraction": max_frac,
            "asset_return_fraction": points[-1][1] / initial_assets - 1,
            "last_metric_t": points[-1][0]}


def describe(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values),
            "std": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values), "max": max(values)}


def bootstrap_mean_ci(values, n_boot=5000):
    if len(values) < 2:
        return None
    rng = random.Random(20261008)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(n_boot))
    return [means[int(n_boot * .025)], means[int(n_boot * .975)]]


def wilson_interval(successes, n):
    if not n:
        return None
    z = 1.959963984540054
    p = successes / n
    center = (p + z*z/(2*n)) / (1 + z*z/n)
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return [max(0.0, center - half), min(1.0, center + half)]


def analyze(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    rows, incomplete, curves = [], [], []
    for i, entry in enumerate(manifest["entries"]):
        if entry.get("state") != "complete":
            incomplete.append({"index": i, "state": entry.get("state"),
                               "error": entry.get("error"), "job": entry["job"]})
            continue
        with gzip.open(directory / entry["artifact"], "rt") as f:
            artifact = json.load(f)
        section = artifact["merchant"]
        status = artifact["status"]
        if status.get("phase") != "finished" or status.get("state") in {"error", "stopped"}:
            raise ValueError("cannot score unfinished run")
        score = compute(section)
        horizon = artifact["scenario"]["run"]["horizon_steps"]
        if section.get("is_alive") and int(status["t"]) < horizon:
            raise ValueError("live run finished before requested horizon")
        initial = sum(float(artifact["scenario"]["run"][k]) for k in ("initial_cash", "initial_deposit"))
        job = entry["job"]
        row = {"index": i, "run_id": entry["run_id"], "mode": job["selection_mode"],
               "seed": job["seed"], "selection_seed": job["selection_seed"],
               "days": job["days"], **score,
               **curve_metrics(section["series"]["net_assets"], initial),
               "wall_seconds": artifact["wall_seconds"],
               "final_t": status["t"], "initial_assets": initial}
        row["survived_horizon"] = bool(row["is_alive"] and row["final_t"] >= horizon)
        listings = section["series"].get("n_active_listings") or []
        row["final_active_listings"] = listings[-1][1] if listings else None
        row["horizon_fraction_with_listings"] = (
            sum(1 for t, n in listings if t < horizon and n > 0) / horizon if listings else None
        )
        # Dead shops can retain stale listing records in drain; count only living operating ticks.
        if listings and row["died_at_t"] is not None:
            row["horizon_fraction_with_listings"] = sum(
                1 for t, n in listings if t < min(horizon, row["died_at_t"]) and n > 0
            ) / horizon
        curve = section["series"]["net_assets"]
        if row["last_metric_t"] != int(status["t"]) - 1:
            raise ValueError("last metric does not match final simulation tick")
        if [int(t) for t, _ in curve] != list(range(int(status["t"]))):
            raise ValueError("missing hourly samples: drawdown would be underestimated")
        peak = initial
        for t, value in [[-1, initial], *curve]:
            peak = max(peak, value)
            curves.append({"run_id": entry["run_id"], "mode": row["mode"],
                           "seed": row["seed"], "t": t,
                           "elapsed_days": (t + 1) * artifact["scenario"]["run"].get("step_hours", 1) / 24,
                           "net_assets": value, "drawdown_fraction": (peak - value) / peak})
        rows.append(row)
    metrics = ["final_net_assets", "net_profit", "shop_rating_mean", "survived_horizon",
               "max_drawdown_absolute", "max_drawdown_fraction", "asset_return_fraction", "wall_seconds",
               "final_active_listings", "horizon_fraction_with_listings"]
    groups = {}
    for mode in sorted({r["mode"] for r in rows}):
        selected = [r for r in rows if r["mode"] == mode]
        groups[mode] = {m: describe([r[m] for r in selected if r[m] is not None]) for m in metrics}
        groups[mode]["survived_horizon"]["wilson_95pct_interval"] = wilson_interval(
            sum(r["survived_horizon"] for r in selected), len(selected))
    by_seed = {}
    for row in rows:
        pair = by_seed.setdefault(row["seed"], {})
        if row["mode"] in pair:
            raise ValueError("duplicate mode/seed; analyze replicates separately")
        pair[row["mode"]] = row
    pairs = []
    for seed, pair in sorted(by_seed.items()):
        if set(pair) != {"random", "daily_report"}:
            continue
        pairs.append({"seed": seed, **{m: pair["daily_report"][m] - pair["random"][m]
                     for m in metrics if pair["daily_report"][m] is not None and pair["random"][m] is not None}})
    paired = {m: {**describe([p[m] for p in pairs if m in p]),
                  "bootstrap_95pct_ci_mean": bootstrap_mean_ci([p[m] for p in pairs if m in p])}
              for m in metrics}
    result = {"protocol": "eval.scoring.compute unchanged; independent diagnostics",
              "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "manifest_fingerprint": manifest.get("fingerprint"),
              "planned": len(manifest["entries"]), "completed": len(rows),
              "incomplete": incomplete, "groups": groups,
              "paired_daily_report_minus_random": paired, "pairs": pairs,
              "daily_reports": manifest["daily_reports"],
              "cautions": ["daily_report includes cash/rating/price/stale-listing filters; not a pure sourcing ablation",
                           "Absent private reports => daily_report is filtered fallback, not report-informed",
                           "Final metrics and drawdown include settlement drain; dead shops remain in sample",
                           "Missing/failed executions excluded, never counted as business deaths",
                           "Survival can reflect an empty shelf; inspect horizon_fraction_with_listings",
                           "Small-sample bootstrap intervals are exploratory; no real-world ROI claim"]}
    (directory / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    if rows:
        with (directory / "metrics.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with (directory / "asset_curves.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(curves[0]))
            writer.writeheader()
            writer.writerows(curves)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = analyze(args.directory)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not result["incomplete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
