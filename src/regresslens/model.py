from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(data: object) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Metric(Strict):
    name: str = Field(min_length=1)
    direction: Literal["higher", "lower"]
    threshold: float = Field(default=0.10, gt=0, lt=1)


class Scenario(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    prompt_tokens: int = Field(default=128, ge=1)
    output_tokens: int = Field(default=32, ge=2)
    concurrency: int = Field(default=8, ge=1)
    requests: int = Field(default=64, ge=4)


class Service(Strict):
    command: list[str] = Field(min_length=1)
    build: list[str] = Field(default_factory=list)
    # Probe inside the runtime used by the service when it has its own Python.
    probe: list[str] = Field(default_factory=list)
    target: str = Field(default="http://127.0.0.1:8000", pattern=r"^http://(?:127\.0\.0\.1|localhost):[0-9]+$")
    startup_seconds: float = Field(default=180, gt=0)
    build_seconds: float = Field(default=1200, gt=0)

    @model_validator(mode="after")
    def verify_no_credentials(self):
        forbidden = {"--api-key", "--token", "--password", "--hf-token", "--authorization"}
        args = self.command + self.build + self.probe
        if any(arg.lower().split("=", 1)[0] in forbidden for arg in args):
            raise ValueError("Credentials are not accepted in saved service arguments")
        return self


class Config(Strict):
    backend: Literal["guidellm", "fixture"] = "guidellm"
    case_type: Literal["controlled_fixture", "controlled_injection", "hardware", "local_smoke"] = "hardware"
    model: str = Field(min_length=1)
    tokenizer: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    tokenizer_revision: str = Field(min_length=1)
    seed: int = Field(default=42, ge=0, le=2**32-4096)
    rounds: int = Field(default=5, ge=3, le=100)
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_draws: int = Field(default=10000, ge=1000, le=50000)
    warmup: bool = True
    cooldown_seconds: float = Field(default=1, ge=0)
    timeout_seconds: int = Field(default=240, ge=1)
    max_commits: int = Field(default=12, ge=0)
    time_budget_seconds: int = Field(default=3600, ge=1)
    max_idle_gpu_utilization: float = Field(default=10, ge=0, le=100)
    max_idle_gpu_temperature: float = Field(default=85, ge=0)
    require_gpu: bool = True
    collect_prometheus: bool = True
    slo: dict[Literal["ttft_ms", "tpot_ms", "e2el_ms"], float] | None = None
    max_error_rate: float = Field(default=0.05, ge=0, le=1)
    # P0 gates one predeclared workload/metric. Auxiliary metrics remain descriptive.
    gate_scenario: str
    gate_metric: str = "ttft_ms_p95"
    metrics: list[Metric] = Field(default_factory=lambda: [
        Metric(name="ttft_ms_p95", direction="lower"),
        Metric(name="tpot_ms_p95", direction="lower"),
        Metric(name="output_tokens_per_second", direction="higher"),
    ], min_length=1)
    scenarios: list[Scenario] = Field(min_length=1)
    service: Service | None = None
    dependency_lock: str | None = None

    @model_validator(mode="after")
    def check_config(self):
        scenario_names = {item.id for item in self.scenarios}
        metric_names = {item.name for item in self.metrics}
        if len(scenario_names) != len(self.scenarios) or len(metric_names) != len(self.metrics):
            raise ValueError("Scenario IDs and metric names must be unique")
        if self.gate_scenario not in scenario_names or self.gate_metric not in metric_names:
            raise ValueError("The primary gate must refer to a configured scenario and metric")
        if (self.backend == "fixture") != (self.case_type == "controlled_fixture"):
            raise ValueError("Fixture data requires case_type=controlled_fixture")
        return self


def read_config(path: Path) -> Config:
    return Config.model_validate(yaml.safe_load(path.read_text()))


class Experiment(Strict):
    id: str
    commit: str
    scenario: str
    pair: int
    seed: int
    comparison: str
    spec_digest: str
    started_at: str
    ended_at: str
    environment: dict
    metrics: dict[str, float]
    distributions: dict[str, dict[str, float]]
    artifacts: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


class Result(Strict):
    scenario: str
    metric: str
    baseline: float | None = None
    candidate: float | None = None
    delta: float | None = None
    interval: tuple[float, float] | None = None
    threshold: float
    confidence: float
    pairs: int = 0
    decision: Literal["PASS", "FAIL", "INCONCLUSIVE", "INVALID"]
    baseline_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
