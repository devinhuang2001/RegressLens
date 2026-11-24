from __future__ import annotations

import json
import random
import shutil
import time
from pathlib import Path

from .model import Config, Scenario
from .runner import StepError, command, float_value, read_local
from .storage import write_json


def guidellm_spec(config: Config, scenario: Scenario, target: str, seed: int,
                  count: int, output: str) -> dict:
    metrics = {"kind": "generative"}
    if config.slo:
        metrics["slo"] = config.slo
    return {
        "spec": {
            "backend": {"kind": "openai_http", "target": target, "model": config.model,
                        "request_format": "/v1/completions",
                        "extras": {"body": {"temperature": 0, "seed": seed}}},
            "profile": {"kind": "concurrent", "streams": [scenario.concurrency]},
            "constraints": [{"kind": "max_requests", "count": count}],
            "tokenizer": {"kind": "huggingface_auto", "model": config.tokenizer,
                          "load_kwargs": {"revision": config.tokenizer_revision}},
            "data": [{"kind": "synthetic_text", "prompt_tokens": scenario.prompt_tokens,
                      "output_tokens": scenario.output_tokens}],
            "data_loader": {"kind": "pytorch", "prefetch_factor": 16},
            "seed": {"kind": "static", "value": seed},
            "outputs": [{"kind": "json", "path": output}],
            "metrics": metrics,
        }
    }


def normalize(payload: dict) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    rows = payload.get("benchmarks")
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Expected one GuideLLM 0.8.1 benchmark per measured run")
    source = rows[0]["metrics"]
    metrics, distributions = {}, {}
    fields = {
        "time_to_first_token_ms": ("ttft_ms", 1),
        "time_per_output_token_ms": ("tpot_ms", 1),
        "request_latency": ("e2e_ms", 1000),
        "inter_token_latency_ms": ("itl_ms", 1),
        "output_token_count": ("output_tokens", 1),
        "prompt_token_count": ("prompt_tokens", 1),
        "output_tokens_per_second": ("output_tokens_per_second", 1),
        "requests_per_second": ("requests_per_second", 1),
    }
    for field, (name, scale) in fields.items():
        status = source.get(field) or {}
        summary = status.get("successful") or (source.get("text") or {}).get(field, {}).get("successful") or {}
        count = float_value(summary.get("count"))
        if not count:
            continue
        stats = {"count": count}
        for key, operand in [("mean", summary.get("mean")), *list((summary.get("percentiles") or {}).items())]:
            value = float_value(operand)
            if (key == "mean" or key in ("p50", "p75", "p90", "p95", "p99")) and value is not None:
                stats[key] = value * scale
        distributions[name] = stats
        if name.endswith("per_second") and "mean" in stats:
            metrics[name] = stats["mean"]
        if name in ("ttft_ms", "tpot_ms", "e2e_ms", "itl_ms") and "p95" in stats:
            metrics[name + "_p95"] = stats["p95"]
    totals = source.get("request_totals") or {}
    total, success = float_value(totals.get("total")), float_value(totals.get("successful"))
    if total and success is not None:
        metrics["error_rate"] = (total - success) / total
        metrics["requests_total"] = total
        metrics["requests_successful"] = success
    attainment = float_value(source.get("slo_attainment"))
    if attainment is not None:
        metrics["slo_attainment"] = attainment
    return metrics, distributions


def prometheus(source: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in source.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        series, raw = parts
        value = float_value(raw)
        if value is not None:
            values[series] = value
    return values


def promote(before: dict, after: dict) -> dict:
    values = {}

    def counter(metric):
        left = {key for key in before if key.split("{", 1)[0] == metric}
        right = {key for key in after if key.split("{", 1)[0] == metric}
        if not left or left != right or any(after[key] < before[key] for key in left):
            return None
        return sum(after[key] - before[key] for key in left)

    for name in ("queue", "prefill", "decode"):
        base = f"vllm:request_{name}_time_seconds"
        total, count = counter(base + "_sum"), counter(base + "_count")
        if count and total is not None:
            values[name + "_ms_mean"] = 1000 * total / count
    for aliases in [("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")]:
        for alias in aliases:
            observations = [value for key, value in after.items()
                            if key.split("{", 1)[0] == alias]
            if observations:
                values["kv_cache_usage_snapshot"] = max(observations)
                break
    preemptions = counter("vllm:num_preemptions_total")
    if preemptions is not None:
        values["preemptions"] = preemptions
    return values


def fetch_metrics(target: str, artifact: Path, phase: str) -> dict:
    try:
        source = read_local(target + "/metrics").decode()
    except (OSError, ValueError):
        return {}
    (artifact / f"prometheus-{phase}.txt").write_text(source)
    return prometheus(source)


def guidellm(config: Config, scenario: Scenario, target: str, seed: int, artifact: Path, remaining):
    executable = shutil.which("guidellm")
    if executable is None:
        raise StepError("ERROR", "GuideLLM 0.8.1 is required for this backend")
    runs = [("measured", scenario.requests)]
    if config.warmup:
        runs.insert(0, ("warmup", max(4, scenario.concurrency * 2)))
    observations = {}
    scrape_seconds = 0.0
    for name, count in runs:
        output = artifact / f"{name}.json"
        spec = artifact / f"{name}-scenario.json"
        write_json(spec, guidellm_spec(config, scenario, target, seed, count, str(output)))
        if name == "measured" and config.collect_prometheus:
            start = time.monotonic()
            before = fetch_metrics(target, artifact, "before")
            scrape_seconds += time.monotonic() - start
        command([executable, "run", "--config", str(spec), "--disable-console",
                 "--disable-console-interactive"], artifact,
                min(config.timeout_seconds, remaining()), artifact / f"{name}.log", "ERROR")
        try:
            metrics, distributions = normalize(json.loads(output.read_text()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise StepError("ERROR", "GuideLLM output missing or incompatible with 0.8.1") from error
        if name == "measured" and config.collect_prometheus:
            start = time.monotonic()
            observations = promote(before, fetch_metrics(target, artifact, "after"))
            scrape_seconds += time.monotonic() - start
    metrics.update(observations)
    write_json(artifact / "collection.json", {"prometheus_boundary_scrape_seconds": scrape_seconds,
                                               "heavy_profiler": False})
    warnings = []
    if metrics.get("requests_total") != scenario.requests:
        warnings.append("Measured request count differs from workload")
    if metrics.get("error_rate", 1) > config.max_error_rate:
        warnings.append("Failed or incomplete request rate exceeded the configured limit")
    return metrics, distributions, warnings


def fixture(state: dict, scenario: Scenario, seed: int, pair: int, artifact: Path):
    if state.get("build_failure"):
        raise StepError("SKIP", "Controlled fixture simulates a failed build")
    rng = random.Random(seed)
    noise_pattern = [-0.26, 0.32, -0.19, 0.35, 0.06]
    delay = state.get("delay", 0.0)
    if delay >= 0.2:
        delay += [-0.018, 0.008, -0.005, 0.015, 0][pair % 5]
    if state.get("noisy"):
        delay = noise_pattern[pair % len(noise_pattern)]
    long = scenario.prompt_tokens > 256
    jitter = 1 + rng.uniform(-0.045, 0.045)
    ttft = (82 if long else 55) * jitter * (1 + delay)
    decode = 12.5 * jitter
    queue = (24 if long else 14) * jitter * (1 + delay * 3.8)
    throughput = 480 / jitter * (1 - delay * 0.52)
    metrics = {"ttft_ms_p95": ttft, "tpot_ms_p95": decode, "e2e_ms_p95": ttft + decode * 32,
               "itl_ms_p95": decode * 1.03, "output_tokens_per_second": throughput,
               "requests_per_second": throughput / scenario.output_tokens, "error_rate": 0,
               "queue_ms_mean": queue, "prefill_ms_mean": (29 if long else 16) * jitter,
               "decode_ms_mean": decode * (scenario.output_tokens - 1), "preemptions": 0,
               "kv_cache_usage_snapshot": 0.58, "slo_attainment": max(0, 1 - delay * 0.45)}
    distributions = {}
    for label in ("ttft_ms", "tpot_ms", "e2e_ms", "itl_ms"):
        value = metrics[label + "_p95"]
        distributions[label] = {"count": scenario.requests, "mean": value * 0.78,
                                "p50": value * 0.74, "p90": value * 0.94,
                                "p95": value, "p99": value * 1.09}
    write_json(artifact / "fixture.json", {"fixture": state, "metrics": metrics,
                                           "distributions": distributions})
    return metrics, distributions, []
