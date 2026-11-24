from __future__ import annotations

from statistics import mean

from .model import Config, Experiment, Result


def diagnose(gate: Result, baseline: list[Experiment], candidate: list[Experiment],
             config: Config, changed: list[str]) -> list[dict]:
    def evidence(metric: str, scenario: str = gate.scenario):
        left = [run for run in baseline if run.scenario == scenario and metric in run.metrics]
        right = [run for run in candidate if run.scenario == scenario and metric in run.metrics]
        if len(left) != config.rounds or len(right) != config.rounds:
            return None
        a, b = mean(run.metrics[metric] for run in left), mean(run.metrics[metric] for run in right)
        return {"metric": metric, "scenario": scenario, "baseline": a, "candidate": b,
                "relative_delta": (b - a) / a if a else None,
                "baseline_ids": [run.id for run in left], "candidate_ids": [run.id for run in right]}

    if gate.decision == "INVALID":
        return [{
            "id": "RCA-005", "category": "Environment / comparability",
            "summary": "The measurement validity checks blocked source attribution.",
            "support": gate.warnings, "against": [], "missing": [],
            "files": [], "next": "Repeat both versions with matching environment, workload and error bounds.",
            "verification": "UNVERIFIED",
        }]
    if gate.decision != "FAIL":
        return []
    shortest = min(config.scenarios, key=lambda s: s.prompt_tokens)
    longest = max(config.scenarios, key=lambda s: s.prompt_tokens)
    findings = []
    groups = [
        ("RCA-001", "Scheduler", "Queueing may account for the first-token slowdown.",
         [
            (evidence("ttft_ms_p95"), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "TTFT p95"),
            (evidence("queue_ms_mean"), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .15, "request queue mean"),
            (evidence("tpot_ms_p95"), lambda x: x["relative_delta"] is not None and abs(x["relative_delta"]) < .10, "TPOT p95"),
         ], ("sched", "admission", "max_num_seqs"), "Restore the scheduler/admission change and repeat the same paired load."),
        ("RCA-002", "KV Cache", "Cache pressure may be increasing request preemptions.",
         [
            (evidence("ttft_ms_p95", longest.id), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "long-prompt TTFT p95"),
            (evidence("preemptions", longest.id), lambda x: x["candidate"] > x["baseline"], "request preemptions"),
            (evidence("kv_cache_usage_snapshot", longest.id), lambda x: x["candidate"] - x["baseline"] >= .05, "end-of-run KV cache occupancy"),
         ], ("kv_cache", "block_pool", "cache"), "Hold concurrency fixed, then vary available KV blocks or revert the cache change."),
        ("RCA-003", "Decode", "The output path may be contributing to per-token latency.",
         [
            (evidence("tpot_ms_p95"), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "TPOT p95"),
            (evidence("decode_ms_mean"), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "request decode mean"),
         ], ("decode", "attention", "kernel", "gemm"), "Repeat a fixed-output workload and capture a separate GPU trace."),
        ("RCA-004", "Prefill", "Long-prompt prefill may explain part of the first-token slowdown.",
         [
            (evidence("ttft_ms_p95", longest.id), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "long-prompt TTFT p95"),
            (evidence("prefill_ms_mean", longest.id), lambda x: x["relative_delta"] is not None and x["relative_delta"] >= .10, "long-prompt prefill mean"),
            (evidence("ttft_ms_p95", shortest.id) if shortest != longest else None,
             lambda x: x["relative_delta"] is not None and x["relative_delta"] < .10, "short-prompt control"),
         ], ("prefill", "attention"), "Repeat short and long prompts at matched output length and concurrency."),
    ]
    for identifier, category, summary, signals, keywords, next_step in groups:
        support, against, missing = [], [], []
        for reading, matches, label in signals:
            if reading is None:
                missing.append(label)
            elif matches(reading):
                support.append(reading)
            else:
                against.append(reading)
        # A change in the primary symptom is required. Supporting secondary
        # observations and counterevidence stay visible even for weak hypotheses.
        if signals[0][0] is None or not signals[0][1](signals[0][0]):
            continue
        findings.append({"id": identifier, "category": category, "summary": summary,
                         "support": support, "against": against, "missing": missing,
                         "files": [path for path in changed if any(key in path.lower() for key in keywords)],
                         "next": next_step, "verification": "UNVERIFIED"})
    return sorted(findings, key=lambda row: (len(row["support"]) - len(row["against"]), len(row["files"])),
                  reverse=True)
