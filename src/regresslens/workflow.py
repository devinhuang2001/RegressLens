from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from . import benchmark
from .diagnosis import diagnose
from .model import Config, Experiment, fingerprint, timestamp
from .runner import (StepError, command, environment, git, gpu_snapshot, history, probe_runtime,
                     revision, serve, worktree)
from .statistics import compare_all
from .storage import Store, write_json


class Budget:
    def __init__(self, seconds: int):
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        seconds = self.end - time.monotonic()
        if seconds <= 0:
            raise StepError("PARTIAL", "Investigation time budget exhausted")
        return seconds

    def cooldown(self, seconds: float):
        if seconds > self.remaining():
            raise StepError("PARTIAL", "Time budget exhausted before cooldown")
        time.sleep(seconds)


def create(store: Store, repo: Path, good: str, bad: str, config: Config, identifier: str | None = None):
    repo = repo.resolve()
    if git(repo, "rev-parse", "--is-bare-repository") != "false":
        raise ValueError("A local, non-bare repository is required")
    good, bad = revision(repo, good), revision(repo, bad)
    commits = history(repo, good, bad)
    inv = {
        "schema_version": 1, "id": identifier or uuid4().hex[:12], "state": "CREATED",
        "created_at": timestamp(), "updated_at": timestamp(), "case_type": config.case_type,
        "repository": str(repo), "good": good, "bad": bad, "config": config.model_dump(mode="json"),
        "commits": [{"sha": sha, "status": "UNTESTED",
                     "subject": git(repo, "show", "-s", "--format=%s", sha)} for sha in commits],
        "decisions": [], "experiments": {}, "findings": [], "verifications": [],
        "localization": None, "gate": None, "metrics": [],
    }
    store.save(inv, create=True)
    write_json(store.path(inv["id"]) / "config.json", inv["config"])
    return inv


def update(store: Store, inv: dict, state: str | None = None):
    if state:
        inv["state"] = state
    inv["updated_at"] = timestamp()
    store.save(inv)


def absolute_artifact(store: Store, inv: dict, comparison: str, side: str, scenario: str, pair: int) -> Path:
    return store.path(inv["id"]) / "runs" / comparison / side / scenario / f"pair-{pair:02d}"


def measure(config: Config, work: Path, commit: str, scenario, index: int,
            comparison: str, artifact: Path, budget: Budget, external_target: str | None = None):
    artifact.mkdir(parents=True, exist_ok=True)
    snapshot = artifact / "experiment.json"
    if snapshot.is_file():
        return Experiment.model_validate_json(snapshot.read_text())
    budget.remaining()
    started_at, seed = timestamp(), config.seed + index
    dirty = bool(git(work, "status", "--porcelain")) if commit != "unmanaged" else False
    if config.backend == "fixture":
        info, warnings = environment(config, work, [], None, [], commit, dirty)
        state = json.loads((work / "scheduler.json").read_text())
        if state.get("environment_drift"):
            warnings.append("Controlled fixture simulates an incompatible environment")
        metrics, distributions, extra = benchmark.fixture(state, scenario, seed, index, artifact)
    elif external_target:
        idle = []
        info, warnings = environment(config, work, idle, None, gpu_snapshot(), commit, dirty)
        metrics, distributions, extra = benchmark.guidellm(config, scenario, external_target, seed,
                                                           artifact, budget.remaining)
    else:
        budget.cooldown(config.cooldown_seconds)
        idle = gpu_snapshot()
        with serve(config, work, artifact, budget.remaining) as target:
            runtime = probe_runtime(config, work, artifact, budget.remaining)
            info, warnings = environment(config, work, idle, runtime, gpu_snapshot(), commit, dirty)
            metrics, distributions, extra = benchmark.guidellm(config, scenario, target, seed,
                                                               artifact, budget.remaining)
    run = Experiment(
        id=f"{comparison}/{artifact.parent.parent.name}/{scenario.id}/{index}", commit=commit,
        pair=index, seed=seed, scenario=scenario.id, comparison=comparison,
        spec_digest=fingerprint({"scenario": scenario.model_dump(mode="json"), "seed": config.seed,
                                 "warmup": config.warmup, "slo": config.slo,
                                 "cooldown_seconds": config.cooldown_seconds}),
        started_at=started_at, ended_at=timestamp(), environment=info, metrics=metrics,
        distributions=distributions, warnings=warnings + extra,
        artifacts={path.stem: str(path.relative_to(artifact.parents[4]))
                   for path in artifact.iterdir() if path.is_file()},
    )
    write_json(snapshot, run.model_dump(mode="json"))
    return run


def run_pair(store: Store, inv: dict, config: Config, left: str, right: str, label: str,
             budget: Budget, revert: str | None = None):
    repo = Path(inv["repository"])
    with worktree(repo, left) as a_path, worktree(repo, right) as b_path:
        target_sha = right
        if revert:
            parents = git(repo, "rev-list", "--parents", "-n", "1", revert).split()
            try:
                if len(parents) != 2:
                    raise ValueError("Controlled revert requires a single-parent commit")
                git(b_path, "revert", "--no-commit", revert)
            except (subprocess.SubprocessError, ValueError) as error:
                raise StepError("BLOCKED", "Change could not be reverted cleanly in the isolated worktree") from error
            patch = git(b_path, "diff", "--cached", "--binary", "HEAD")
            if not patch:
                raise StepError("BLOCKED", "Revert produced no patch")
            patch_file = store.path(inv["id"]) / f"{label}.patch"
            patch_file.write_text(patch + "\n")
            target_sha = right + "+revert:" + revert
        if config.service and config.service.build:
            for side, path in (("baseline", a_path), ("candidate", b_path)):
                missing = any(not (absolute_artifact(store, inv, label, side, scenario.id, index)
                                   / "experiment.json").exists()
                              for scenario in config.scenarios for index in range(config.rounds))
                if missing:
                    output = store.path(inv["id"]) / "runs" / label / side
                    output.mkdir(parents=True, exist_ok=True)
                    command(config.service.build, path,
                            min(config.service.build_seconds, budget.remaining()),
                            output / "build.log", "SKIP")
        collected: dict[str, list[Experiment]] = {"baseline": [], "candidate": []}
        for scenario in config.scenarios:
            for index in range(config.rounds):
                teams = [("baseline", a_path, left), ("candidate", b_path, target_sha)]
                if index % 2:
                    teams.reverse()
                for side, path, sha in teams:
                    artifact = absolute_artifact(store, inv, label, side, scenario.id, index)
                    run = measure(config, path, sha, scenario, index, label, artifact, budget)
                    collected[side].append(run)
                    inv["experiments"][run.id] = run.model_dump(mode="json")
                    update(store, inv)
    return collected["baseline"], collected["candidate"]


def evaluate(store, inv, config, left, right, label, budget):
    a, b = run_pair(store, inv, config, left, right, label, budget)
    gate, metrics = compare_all(a, b, config)
    return gate, metrics, a, b


def summarize(inv: dict) -> tuple[int, int, bool]:
    statuses = [row["status"] for row in inv["commits"]]
    passed = [index for index, item in enumerate(statuses) if item == "PASS"]
    failed = [index for index, item in enumerate(statuses) if item == "FAIL"]
    return max(passed), min(failed), max(passed) >= min(failed)


def record(store, inv, row, status, gate=None, reason=None):
    row.update({"status": status, "reason": reason, "gate": gate.model_dump(mode="json") if gate else None})
    inv["decisions"].append({"sha": row["sha"], "status": status, "reason": reason,
                             "gate": row["gate"], "at": timestamp()})
    update(store, inv)


def run(store: Store, identifier: str, *, trusted: bool = False):
    with store.lock(identifier):
        inv = store.get(identifier)
        config = Config.model_validate(inv["config"])
        if config.backend == "guidellm" and (not trusted or not config.service):
            raise ValueError("Managed execution requires a service and --trusted-repo for reviewed local code")
        if inv["state"] == "COMPLETED":
            return inv
        if inv["state"] == "VERIFYING":
            raise ValueError("Resume an interrupted verification with the same verify command")
        inv.pop("error", None)
        budget = Budget(config.time_budget_seconds)
        commits = inv["commits"]
        try:
            update(store, inv, "ENV_CHECK")
            if config.require_gpu and not gpu_snapshot():
                raise StepError("INVALID", "Required NVIDIA GPU is unavailable")
            update(store, inv, "BENCHMARKING")
            end_gate, end_metrics, left, right = evaluate(
                store, inv, config, inv["good"], inv["bad"], "endpoints", budget)
            inv.update(gate=end_gate.model_dump(mode="json"),
                       metrics=[item.model_dump(mode="json") for item in end_metrics])
            update(store, inv, "REGRESSION_CHECK")
            if end_gate.decision != "FAIL":
                inv["findings"] = diagnose(end_gate, left, right, config, [])
                update(store, inv, {"INVALID": "FAILED", "INCONCLUSIVE": "INCONCLUSIVE"}.get(
                    end_gate.decision, "COMPLETED"))
                return inv
            if commits[0]["status"] == "UNTESTED":
                record(store, inv, commits[0], "PASS", reason="Reference endpoint in a valid endpoint comparison")
            if commits[-1]["status"] == "UNTESTED":
                record(store, inv, commits[-1], "FAIL", end_gate)
            update(store, inv, "BISECTING")
            attempted = sum(row["status"] != "UNTESTED" for row in commits[1:-1])
            while attempted < config.max_commits:
                low, high, contradiction = summarize(inv)
                untested = [i for i in range(low + 1, high) if commits[i]["status"] == "UNTESTED"]
                if contradiction or not untested:
                    break
                index = min(untested, key=lambda i: abs(i - (low + high) / 2))
                row = commits[index]
                sha = row["sha"]
                try:
                    gate, _, _, _ = evaluate(store, inv, config, inv["good"], sha, "commit-" + sha[:12], budget)
                except StepError as error:
                    if error.status == "PARTIAL":
                        raise
                    record(store, inv, row, error.status, reason=str(error))
                else:
                    record(store, inv, row, gate.decision, gate)
                attempted += 1
            localize(inv)
            update(store, inv, "DIAGNOSING")
            bound = inv["localization"]
            diff_left, diff_right = bound["last_pass"], bound["first_fail"]
            repo = Path(inv["repository"])
            changed = git(repo, "diff", "--no-ext-diff", "--name-only", diff_left, diff_right).splitlines()
            diff = git(repo, "diff", "--no-ext-diff", "--no-textconv", diff_left, diff_right)
            (store.path(identifier) / "source.diff").write_text(diff + "\n")
            inv["source_diff"] = diff[:12000]
            inv["source_diff_ref"] = "source.diff"
            inv["changed_files"] = changed
            # Attribution compares the reference with the earliest measured FAIL.
            # This keeps the observation aligned with the patch range shown.
            if diff_right != inv["bad"]:
                prefix = "commit-" + diff_right[:12] + "/"
                runs = [Experiment.model_validate(item) for key, item in inv["experiments"].items()
                        if key.startswith(prefix)]
                diagnostic_left = [item for item in runs if "/baseline/" in item.id]
                diagnostic_right = [item for item in runs if "/candidate/" in item.id]
                diagnostic_gate, _ = compare_all(diagnostic_left, diagnostic_right, config)
            else:
                diagnostic_left, diagnostic_right, diagnostic_gate = left, right, end_gate
            inv["diagnostic_comparison"] = {"baseline": inv["good"], "candidate": diff_right}
            inv["findings"] = diagnose(diagnostic_gate, diagnostic_left, diagnostic_right, config, changed)
            verified = {row["hypothesis"]: row["status"] for row in inv["verifications"]}
            for finding in inv["findings"]:
                finding["verification"] = verified.get(finding["id"], finding["verification"])
            update(store, inv, "COMPLETED" if bound["status"] == "LOCALIZED" else "PARTIAL")
        except StepError as error:
            inv["error"] = str(error)
            if error.status == "INVALID":
                inv["gate"] = {"decision": "INVALID", "warnings": [str(error)]}
            if commits[0]["status"] == "PASS" and commits[-1]["status"] == "FAIL":
                localize(inv)
            update(store, inv, "PARTIAL" if error.status == "PARTIAL" else "FAILED")
        except KeyboardInterrupt:
            update(store, inv, "CANCELLED")
            raise
        except (ValueError, OSError, subprocess.SubprocessError):
            inv["error"] = "Investigation interrupted by invalid input or a process/filesystem failure"
            update(store, inv, "FAILED")
            raise
        else:
            inv.pop("error", None)
            update(store, inv)
        return inv


def localize(inv: dict):
    low, high, contradiction = summarize(inv)
    commits = inv["commits"]
    inv["localization"] = {
        "status": "NON_MONOTONIC" if contradiction else ("LOCALIZED" if high == low + 1 else "PARTIAL"),
        "last_pass": commits[low]["sha"], "first_fail": commits[high]["sha"],
        "candidates": [row["sha"] for row in commits[low + 1:high + 1]],
        "assumption": "One persistent crossing of the configured threshold on the first-parent history",
    }


def log_verification(store: Store, inv: dict, hypothesis: str, row: dict):
    finding = next((item for item in inv["findings"] if item["id"] == hypothesis), None)
    if finding is None:
        raise ValueError("Verification requires an existing hypothesis")
    finding["verification"] = row["status"]
    inv["verifications"].append({**row, "hypothesis": hypothesis, "at": timestamp()})
    return inv


def request_verification(store: Store, identifier: str, hypothesis: str, reason: str) -> dict:
    if not 1 <= len(reason) <= 2000:
        raise ValueError("Verification reason must contain 1–2000 characters")
    with store.lock(identifier):
        inv = store.get(identifier)
        finding = next((row for row in inv["findings"] if row["id"] == hypothesis), None)
        if finding is None:
            raise ValueError("Unknown hypothesis")
        if finding["verification"] in ("SUPPORTED", "REFUTED"):
            raise ValueError("The hypothesis already has a completed validation")
        log_verification(store, inv, hypothesis,
                         {"status": "REQUESTED", "method": "manual", "reason": reason, "evidence": []})
        update(store, inv)
        return inv["verifications"][-1]


def verify(store: Store, identifier: str, hypothesis: str, suspect: str, *, trusted: bool):
    with store.lock(identifier):
        inv = store.get(identifier)
        if inv["state"] not in ("COMPLETED", "PARTIAL", "VERIFYING"):
            raise ValueError("Verification requires an investigated regression")
        if hypothesis not in {item["id"] for item in inv["findings"]}:
            raise ValueError("Unknown RCA hypothesis")
        config = Config.model_validate(inv["config"])
        if config.backend == "guidellm" and not trusted:
            raise ValueError("Review the repository and pass --trusted-repo to run verification")
        if not re.fullmatch(r"[a-f0-9]{40,64}", suspect) or suspect not in (inv["localization"] or {}).get("candidates", []):
            raise ValueError("Supply the full SHA of a candidate in the localized interval")
        return_state = "PARTIAL" if inv["localization"]["status"] != "LOCALIZED" else "COMPLETED"
        update(store, inv, "VERIFYING")
        budget = Budget(config.time_budget_seconds)
        provenance = {"method": "isolated_revert", "commit": suspect, "evidence": []}
        try:
            good, reverted = run_pair(store, inv, config, inv["good"], inv["bad"],
                                      f"restore-{suspect[:12]}", budget, revert=suspect)
            restoration, _ = compare_all(good, reverted, config)
            bad, rollback = run_pair(store, inv, config, inv["bad"], inv["bad"],
                                     f"recovery-{suspect[:12]}", budget, revert=suspect)
            improvement, _ = compare_all(bad, rollback, config)
            provenance["evidence"] = [restoration.model_dump(mode="json"), improvement.model_dump(mode="json")]
            controlled = bool(improvement.interval and improvement.interval[1] < 0)
            status = "SUPPORTED" if restoration.decision == "PASS" and controlled else "INCONCLUSIVE"
            if improvement.interval and improvement.interval[0] >= 0 and restoration.decision == "FAIL":
                status = "REFUTED"
            provenance["reason"] = ("A separate Bad/revert pair tests improvement and a Good/revert pair "
                                    "checks whether residual regression is below the original threshold.")
        except StepError as error:
            status = "BLOCKED"
            provenance["reason"] = str(error)
        except BaseException:
            update(store, inv, return_state)
            raise
        log_verification(store, inv, hypothesis, {**provenance, "status": status})
        update(store, inv, return_state)
        return inv
