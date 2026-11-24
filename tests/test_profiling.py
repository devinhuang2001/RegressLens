from contextlib import contextmanager
import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regresslens.api import app_for
from regresslens.profile_capture import capture
from regresslens.profiling import attach, chrome_trace, flame_nodes, get_profile, speedscope
from regresslens.report import report_data, standalone
from regresslens.storage import Store


GOOD = "a" * 40
BAD = "b" * 40


def trace(duration):
    return {"traceEvents": [
        {"ph": "M", "pid": 1, "tid": 1, "name": "thread_name", "args": {"name": "scheduler"}},
        {"ph": "B", "pid": 1, "tid": 1, "name": "engine", "ts": 20, "cat": "python_function"},
        {"ph": "X", "pid": 1, "tid": 1, "name": "queue", "ts": 21, "dur": 2, "cat": "python_function"},
        {"ph": "E", "pid": 1, "tid": 1, "ts": 30},
        {"ph": "X", "pid": 0, "tid": 7, "name": "attention", "ts": 35, "dur": duration, "cat": "kernel"},
    ]}


@pytest.fixture
def investigation(tmp_path):
    store = Store(tmp_path / "store")
    from regresslens.model import Config, Service, Scenario

    cfg = Config(model="fixture-model", model_revision="rev", tokenizer="fixture-tokenizer",
                 tokenizer_revision="rev", scenarios=[Scenario(id="test")], gate_scenario="test",
                 service=Service(command=["vllm", "serve", "fixture-model"]))
    store.save({"id": "case", "created_at": "", "good": GOOD, "bad": BAD, "repository": ".",
                "state": "COMPLETED", "case_type": "hardware", "config": cfg.model_dump(mode="json"),
                "gate": {"decision": "FAIL"}, "localization": {},
                "commits": [{"sha": GOOD}, {"sha": BAD}], "decisions": [], "findings": [],
                "verifications": [], "metrics": [], "experiments": {}}, create=True)
    return store


def test_trace_handles_gzip_nesting_kernel_delta_and_atomic_attachment(investigation, tmp_path):
    paths = {}
    for side, duration in (("good", 5), ("bad", 8)):
        paths[side] = tmp_path / f"{side}.json.gz"
        with gzip.open(paths[side], "wt") as f:
            json.dump(trace(duration), f)
    profile = attach(investigation, "case", traces=paths)
    assert profile["traces"]["good"]["events"][0]["label"] == "scheduler"
    assert profile["traces"]["good"]["events"][0]["at"] == 0
    assert profile["kernels"][0]["bad"]["total_us"] - profile["kernels"][0]["good"]["total_us"] == 3
    assert profile["flame"]["source"] == "python_spans"
    assert profile["flame"]["nodes"][0]["good"] == 10
    case = investigation.get("case")
    assert case["gate"] == {"decision": "FAIL"}
    paths["bad"].write_text("{}")
    with pytest.raises(ValueError, match="Chrome Trace"):
        attach(investigation, "case", traces=paths)
    assert investigation.get("case") == case
    assert get_profile(investigation, case) == profile
    client = TestClient(app_for(investigation), base_url="http://localhost")
    assert client.get("/api/investigations/case").json()["profiling"] == profile
    response = client.get("/api/investigations/case/profiles/good-trace")
    assert response.content == paths["good"].read_bytes()
    assert response.headers["content-disposition"].startswith("attachment;")
    assert client.get("/api/investigations/case/profiles/not-an-artifact").status_code == 404
    assert "Independent profiling" in standalone(report_data(investigation, case))


def test_crossing_python_spans_are_excluded():
    payload = [
        {"ph": "X", "cat": "python_function", "ts": 0, "dur": 4, "name": "a"},
        {"ph": "X", "cat": "python_function", "ts": 2, "dur": 4, "name": "b"},
    ]
    normalized, _, stacks = chrome_trace(payload)
    assert normalized["skipped_stack_lanes"] == 1
    assert not stacks.weights


def test_fractional_kineto_endpoints_tolerate_clock_rounding():
    epoch_us = 4_646_369_245_000
    payload = [
        {"ph": "X", "cat": "python_function", "ts": epoch_us, "dur": 4, "name": "parent"},
        {"ph": "X", "cat": "python_function", "ts": epoch_us + 2, "dur": 2.001, "name": "child"},
    ]
    trace, _, stacks = chrome_trace(payload)
    assert trace["skipped_stack_lanes"] == 0
    assert sum(stacks.weights.values()) == 4
    assert len(stacks.weights) == 2


def test_speedscope_sampled_and_evented_normalize_to_same_inclusive_tree():
    frames = {"frames": [{"name": "scheduler"}, {"name": "allocate"}]}
    sampled = speedscope({"shared": frames, "profiles": [
        {"type": "sampled", "unit": "milliseconds", "samples": [[0], [0, 1]], "weights": [3, 2]},
    ]})
    evented = speedscope({"shared": frames, "profiles": [
        {"type": "evented", "unit": "microseconds", "startValue": 0, "endValue": 5000, "events": [
            {"type": "O", "frame": 0, "at": 0}, {"type": "O", "frame": 1, "at": 1000},
            {"type": "C", "frame": 1, "at": 3000}, {"type": "C", "frame": 0, "at": 5000},
        ]},
    ]})
    nodes = flame_nodes(sampled, evented)["nodes"]
    assert [row["good"] for row in nodes] == [5000, 5000, 2000]
    assert [row["bad"] for row in nodes] == [5000, 5000, 2000]


def test_managed_capture_replays_same_workload_and_stops_profiler(monkeypatch, investigation):
    from regresslens import profile_capture

    calls = []
    paths = []

    @contextmanager
    def worktree(repo, sha):
        calls.append(sha)
        yield Path(".")

    @contextmanager
    def serve(cfg, work, output, remaining, *, extra_args):
        settings = json.loads(extra_args[1])
        assert extra_args[0] == "--profiler-config"
        assert settings["torch_profiler_with_stack"] is True
        paths.append(Path(settings["torch_profiler_dir"]))
        yield "http://127.0.0.1:8000"

    def control(url, timeout, *, method):
        assert method == "POST"
        calls.append(url.rsplit("/", 1)[1])
        if url.endswith("stop_profile"):
            (paths[-1] / "rank0.pt.trace.json").write_text(json.dumps(trace(5)))

    def replay(cfg, workload, target, seed, output, remaining):
        assert not cfg.collect_prometheus
        assert workload.requests == 10
        calls.append(output.name)
        return {}, {}, []

    monkeypatch.setattr(profile_capture, "worktree", worktree)
    monkeypatch.setattr(profile_capture, "serve", serve)
    monkeypatch.setattr(profile_capture, "read_local", control)
    monkeypatch.setattr(profile_capture.benchmark, "guidellm", replay)
    profile = capture(investigation, "case", trusted=True, bad_commit=None,
                      scenario=None, request_count=10, trace_index=0)
    assert profile["source"] == "vllm_torch_replay"
    assert calls == [GOOD, "warmup", "start_profile", "measured", "stop_profile",
                     BAD, "warmup", "start_profile", "measured", "stop_profile"]
