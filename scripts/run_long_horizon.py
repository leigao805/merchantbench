#!/usr/bin/env python3
"""Sequential, resumable rule-based experiments against a local env server."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import gzip
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import time
from pathlib import Path

import run_batch as batch
from analyze_long_horizon import analyze
from data.daily_reports import resolve_report_dir, report_path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def reports(scenario, days):
    start = date.fromisoformat(scenario["run"]["virtual_time"]["start_date"])
    root = resolve_report_dir(scenario.get("data", {}).get("daily_report_dir"))
    found = {}
    for offset in range(days):
        path = Path(report_path(root, start + timedelta(days=offset)))
        if path.is_file():
            found[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"expected_days": days, "available_days": len(found), "sha256": found,
            "interpretation": "report files present; inspect relevance" if found else "NO REPORTS: filtered fallback baseline"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", default="experiments/long_horizon/queue.yaml")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--days", type=int, help="smoke/pilot override, logged in manifest")
    parser.add_argument("--seeds", type=int, nargs="+", help="subset of queue seeds")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--require-reports", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=14400)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    config = batch.load_queue_config(args.queue)
    jobs = batch.jobs_from_config(config, days_override=args.days)
    if args.seeds is not None:
        unknown = set(args.seeds) - {j["seed"] for j in jobs}
        if unknown:
            parser.error(f"seeds absent from queue: {sorted(unknown)}")
        jobs = [j for j in jobs if j["seed"] in args.seeds]
    if not jobs or any(j["bootstrap_agent"] != "rule_based" for j in jobs):
        parser.error("nonempty rule_based queue required; LLM runs require an explicit budgeted workflow")
    if len({(j["seed"], j["selection_mode"]) for j in jobs}) != len(jobs):
        parser.error("duplicate mode/seed")
    if len({j["days"] for j in jobs}) != 1 or len({j["scenario_path"] for j in jobs}) != 1:
        parser.error("all jobs must share days and scenario")
    if float(config["poll_seconds"]) <= 0:
        parser.error("poll_seconds must be positive")
    scenario = batch._load_scenario_for_path(jobs[0]["scenario_path"])
    batch._set_scenario_run_days(scenario, jobs[0]["days"])
    coverage = reports(scenario, jobs[0]["days"])
    print(json.dumps(coverage), flush=True)
    if args.require_reports and coverage["available_days"] != coverage["expected_days"]:
        parser.error("daily report coverage is incomplete; nothing submitted")
    source_hashes = {str(p.relative_to(batch.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for folder in ("env", "agent", "eval", "scripts")
                     for p in (batch.ROOT / folder).rglob("*.py")}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=batch.ROOT, text=True).strip()
    packages = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    spec = {"jobs": jobs, "scenario": scenario, "source_sha256": source_hashes,
            "daily_reports": coverage, "packages": packages, "python": platform.python_version(),
            "base_url": config["base_url"]}
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            parser.error("output exists; use --resume or a fresh output directory")
        manifest = json.loads(manifest_path.read_text())
        if manifest["fingerprint"] != digest(spec):
            parser.error("resume fingerprint mismatch: configuration/code/data/runtime changed")
    else:
        if args.resume:
            parser.error("no manifest to resume")
        args.output.mkdir(parents=True, exist_ok=True)
        manifest = {"fingerprint": digest(spec), **spec, "git_commit": commit,
                    "platform": platform.platform(),
                    "git_status": subprocess.check_output(["git", "status", "--short"], cwd=batch.ROOT, text=True),
                    "entries": [{"job": j, "state": "pending"} for j in jobs]}
        save(manifest_path, manifest)
    failed = False
    for index, entry in enumerate(manifest["entries"]):
        if entry["state"] == "complete":
            continue
        if entry["state"] in {"failed", "submitting"}:
            print(f"Refusing automatic resubmit of entry {index}: inspect server /runs and manifest.", flush=True)
            failed = True
            continue
        job = entry["job"]
        try:
            if not entry.get("run_id"):
                entry.update(state="submitting", started_at=time.time())
                save(manifest_path, manifest)
                entry["run_id"] = batch.create_run(config["base_url"], job)
                entry["state"] = "running"
                save(manifest_path, manifest)
            rid = entry["run_id"]
            last = None
            deadline = time.monotonic() + args.timeout_seconds
            while True:
                status = batch.get_run_status(config["base_url"], rid)
                progress = (status.get("state"), status.get("phase"), int(status.get("t", 0)) // 240)
                if progress != last:
                    print(f"{index + 1}/{len(jobs)} {job['selection_mode']} seed={job['seed']} {rid}: {status.get('phase')} t={status.get('t')}", flush=True)
                    last = progress
                if status.get("state") in {"stopped", "error"}:
                    entry.update(state="failed", status=status, error="execution failed, not business death")
                    failed = True
                    break
                if status.get("phase") == "finished":
                    merchant = batch.request_json("GET", config["base_url"], f"/runs/{rid}/agents/agent_0/sections/merchant")
                    actual_scenario = dict(scenario)
                    actual_scenario["run"] = {**scenario["run"], "master_seed": job["seed"]}
                    artifact = {"job": job, "scenario": actual_scenario, "status": status,
                                "merchant": merchant, "wall_seconds": time.time() - entry["started_at"]}
                    name = f"{index:03d}-{job['selection_mode']}-{job['seed']}.json.gz"
                    tmp = args.output / (name + ".tmp")
                    with gzip.open(tmp, "wt", encoding="utf-8") as f:
                        json.dump(artifact, f, ensure_ascii=False)
                    tmp.replace(args.output / name)
                    entry.update(state="complete", artifact=name, status=status)
                    entry.pop("error", None)
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(f"run {rid} remains active; resume to reattach or stop it via dashboard")
                time.sleep(float(config["poll_seconds"]))
            save(manifest_path, manifest)
        except Exception as exc:
            entry["error"] = str(exc)
            save(manifest_path, manifest)
            print(f"Execution interrupted: {exc}. No new runs submitted.", flush=True)
            analyze(args.output)
            return 1
    summary = analyze(args.output)
    print(f"Completed {summary['completed']}/{summary['planned']}; {args.output / 'summary.json'}")
    return 1 if failed or summary["incomplete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
