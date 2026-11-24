from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def invoke(store, *argv, code=0):
    result = subprocess.run([sys.executable, "-m", "regresslens", "--json", "--store", str(store), *map(str, argv)],
                            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert result.returncode == code, (result.stdout, result.stderr)
    message = json.loads(result.stdout)
    assert message["schema_version"] == 1
    assert message["ok"] == (code == 0)
    return message


def test_process_contract_and_artifact_boundary(tmp_path):
    store = tmp_path / "store"
    response = invoke(store, "capabilities")
    commands = {tuple(row["argv"]) for row in response["result"]["commands"]}
    assert {("artifact", "export"), ("profile", "capture"), ("request-verification",)} <= commands
    assert invoke(store, "status", code=2)["error"]["code"] == "usage_error"
    assert invoke(store, "status", "--investigation", "missing", code=2)["error"]["code"] == "not_found"
    assert invoke(store, "serve", code=2)["error"]["code"] == "invalid_input"
    assert invoke(store, "list")["result"] == []


def test_agent_can_replay_and_export_the_evidence_chain(tmp_path):
    destination = tmp_path / "demo"
    subprocess.run([sys.executable, str(ROOT / "demo/profiling_fixture.py"), "--out", str(destination)],
                   cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                   capture_output=True, check=True)
    store = destination / "store"
    original = invoke(store, "status", "--investigation", "regression")["result"]
    assert original["gate"]["decision"] == "FAIL" and original["has_profiling"]
    repo = destination / "repositories/regression"
    config = ROOT / "configs/fixture.yaml"
    data = invoke(store, "report", "--investigation", "regression", "--format", "json")["result"]["content"]
    created = invoke(store, "create", "--id", "agent", "--repo", repo,
                     "--config", config, "--good", data["good"], "--bad", data["bad"])["result"]
    assert created["state"] == "CREATED"
    target = ("--investigation", "agent")
    measured = invoke(store, "resume", *target)["result"]
    assert measured["state"] == "COMPLETED" and measured["gate"]["decision"] == "FAIL"
    assert measured["localization"]["first_fail"] == original["localization"]["first_fail"]
    attempted = invoke(store, "inspect", *target, "--section", "commits")["result"]["decisions"]
    assert any(row["status"] == "SKIP" for row in attempted)
    findings = invoke(store, "diagnose", *target)["result"]
    hypothesis = next(row["id"] for row in findings if row["id"] == "RCA-001")
    requested = invoke(store, "request-verification", *target, "--hypothesis", hypothesis,
                       "--reason", "Compare against isolated revert")["result"]
    assert requested["status"] == "REQUESTED"
    confirmed = invoke(store, "verify", *target, "--hypothesis", hypothesis,
                       "--revert", measured["localization"]["first_fail"])["result"]
    assert confirmed["status"] == "SUPPORTED"
    repeated = invoke(store, "request-verification", *target, "--hypothesis", hypothesis,
                      "--reason", "Already verified", code=2)
    assert repeated["error"]["code"] == "invalid_input"
    measures = invoke(store, "inspect", *target, "--section", "runs")["result"]
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    for path, side in ((baseline, "baseline"), (candidate, "candidate")):
        path.write_text(json.dumps([record for key, record in measures.items() if key.startswith(f"endpoints/{side}/")]))
    comparison = invoke(store, "compare", "--config", config,
                        "--baseline", baseline, "--candidate", candidate)["result"]
    assert comparison["gate"]["decision"] == "FAIL"
    run_id = measured["gate"]["candidate_ids"][0]
    one_run = invoke(store, "inspect", *target, "--section", "runs", "--run-id", run_id)["result"]
    assert one_run["id"] == run_id
    inputs = destination / "profiling-inputs"
    attached = invoke(store, "profile", "attach", *target, "--good-trace", inputs / "good.pt.trace.json.gz",
                      "--bad-trace", inputs / "bad.pt.trace.json.gz", "--good-stacks", inputs / "good.speedscope.json",
                      "--bad-stacks", inputs / "bad.speedscope.json")["result"]
    profile = invoke(store, "inspect", *target, "--section", "profiling")["result"]
    assert profile["traces"]["good"]["events"] and profile["flame"]["nodes"] and profile["kernels"]
    files = {row["path"]: row for row in invoke(store, "artifact", "list", *target)["result"]}
    raw = "profiles/" + attached["artifacts"]["good-trace"]["name"]
    output = tmp_path / "original.pt.trace.json.gz"
    copied = invoke(store, "artifact", "export", *target, "--path", raw, "--out", output)["result"]
    assert copied["size_bytes"] == files[raw]["size_bytes"]
    assert output.read_bytes() == (inputs / "good.pt.trace.json.gz").read_bytes()
    assert invoke(store, "artifact", "export", *target, "--path", raw, "--out", output,
                  code=2)["error"]["code"] == "already_exists"
    assert invoke(store, "artifact", "export", *target, "--path", "../index.sqlite3", "--out", tmp_path / "bad",
                  code=2)["error"]["code"] == "invalid_input"
    report = tmp_path / "agent.html"
    assert invoke(store, "report", *target, "--out", report)["result"]["path"] == str(report)
    assert "investigation-data" in report.read_text()
    assert {row["id"] for row in invoke(store, "list")["result"]} == {"agent", "regression"}
