"""Run a separate, opt-in vLLM profiling replay for a managed investigation."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from urllib.error import URLError

from . import benchmark
from .model import Config
from .profiling import attach, selected_commits
from .runner import StepError, command, read_local, serve, worktree
from .storage import Store
from .workflow import Budget


def control(target: str, action: str, timeout: float):
    try:
        read_local(f"{target}/{action}", timeout=timeout, method="POST")
    except (OSError, URLError, ValueError) as error:
        raise StepError("ERROR", f"vLLM {action} failed. Check that the launch command forwards --profiler-config.") from error


def capture(store: Store, identifier: str, *, trusted: bool, bad_commit: str | None,
            scenario: str | None, request_count: int | None, trace_index: int):
    inv = store.get(identifier)
    config = Config.model_validate(inv["config"])
    if not trusted or config.backend != "guidellm" or not config.service:
        raise ValueError("Managed vLLM profiling requires a service configuration and --trusted-repo")
    localized = inv.get("localization") or {}
    candidate = bad_commit or localized.get("first_fail") or inv["bad"]
    commits = selected_commits(inv, candidate)
    workload = next((row for row in config.scenarios
                     if row.id == (scenario or config.gate_scenario)), None)
    if workload is None:
        raise ValueError("Profiling scenario is outside this investigation")
    count = request_count if request_count is not None else max(workload.concurrency, 8)
    if not workload.concurrency <= count <= 128 or trace_index < 0:
        raise ValueError("Profiling needs concurrency ≤ requests ≤ 128 and a nonnegative trace index")
    replay = config.model_copy(update={"collect_prometheus": False, "warmup": False})
    scenario = workload.model_copy(update={"requests": count})
    budget = Budget(config.time_budget_seconds)
    paths = {}
    profile_root = store.path(identifier) / "profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    # Keep capture logs and all worker traces for inspection, including failed
    # replays. Only a successfully attached pair changes the active report.
    capture_root = Path(tempfile.mkdtemp(dir=profile_root, prefix="capture-"))
    for side, sha in commits.items():
        output = capture_root / side
        output.mkdir()
        with worktree(Path(inv["repository"]), sha) as path:
            if config.service.build:
                command(config.service.build, path, min(config.service.build_seconds, budget.remaining()),
                        output / "build.log", "ERROR")
            settings = {"profiler": "torch", "torch_profiler_dir": str(output), "torch_profiler_with_stack": True}
            with serve(config, path, output, budget.remaining,
                       extra_args=["--profiler-config", json.dumps(settings)]) as target:
                for stage in ("warmup", "measured"):
                    stage_dir = output / stage
                    stage_dir.mkdir()
                    if stage == "warmup":
                        if not config.warmup:
                            continue
                        _, _, warnings = benchmark.guidellm(
                            replay, scenario, target, config.seed, stage_dir, budget.remaining)
                    else:
                        control(target, "start_profile", min(15, budget.remaining()))
                        try:
                            _, _, warnings = benchmark.guidellm(
                                replay, scenario, target, config.seed, stage_dir, budget.remaining)
                        finally:
                            # Complete the flush even if the replay fails or
                            # its budget expires after profiling started.
                            control(target, "stop_profile", config.timeout_seconds)
                    if warnings:
                        raise StepError("ERROR", f"Profiling replay failed validity checks: {', '.join(warnings)}")
        candidates = sorted(p for p in output.glob("*.pt.trace.json*")
                            if p.is_file() and "async_llm" not in p.name.lower())
        if trace_index >= len(candidates):
            raise StepError("ERROR", f"{side.title()} worker trace index {trace_index} is unavailable. See {output}.")
        paths[side] = candidates[trace_index]
    return attach(store, identifier, traces=paths, bad_commit=commits["bad"],
                  scenario=workload.id, source="vllm_torch_replay", requests=count)
