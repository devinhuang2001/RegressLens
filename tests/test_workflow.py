from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from regresslens.api import app_for
from regresslens.model import read_config
from regresslens.runner import StepError, git
from regresslens.storage import Store
from regresslens.workflow import create, run, verify

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("fixture", ROOT / "demo/fixture.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


@pytest.mark.parametrize(("case", "decision"), [
    ("regression", "FAIL"), ("stable", "PASS"), ("noisy", "INCONCLUSIVE"), ("environment", "INVALID"),
])
def test_end_to_end(tmp_path, case, decision):
    config = read_config(ROOT / "configs/fixture.yaml")
    repo = tmp_path / "repo"
    shas = fixture.fixture_repo(repo, case)
    store = Store(tmp_path / "store")
    create(store, repo, shas[0], shas[-1], config, "case")
    record = run(store, "case")
    assert record["gate"]["decision"] == decision
    if case == "regression":
        assert record["localization"]["first_fail"] == shas[6]
        assert record["localization"]["last_pass"] == shas[5]
        assert record["localization"]["status"] == "LOCALIZED"
        assert any(row["sha"] == shas[4] and row["status"] == "SKIP" for row in record["decisions"])
        record = verify(store, "case", "RCA-001", shas[6], trusted=False)
        assert record["verifications"][-1]["status"] == "SUPPORTED"
        assert record["findings"][0]["verification"] == "SUPPORTED"
        assert git(repo, "rev-parse", "HEAD") == shas[-1]
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "worktree", "list").count("\n") == 0
    else:
        assert record["localization"] is None
        assert record["decisions"] == []
    for experiment in record["experiments"].values():
        for path in experiment["artifacts"].values():
            assert (store.path("case") / path).is_file()


def test_budget_resume_keeps_saved_runs(tmp_path, monkeypatch):
    import regresslens.workflow as workflow

    repo = tmp_path / "repo"
    shas = fixture.fixture_repo(repo, "regression")
    store = Store(tmp_path / "store")
    cfg = read_config(ROOT / "configs/fixture.yaml")
    create(store, repo, shas[0], shas[-1], cfg, "resume")
    original = workflow.measure
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        if count == 3:
            raise StepError("PARTIAL", "Test budget exhausted")
        count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "measure", interrupted)
    result = run(store, "resume")
    assert result["state"] == "PARTIAL"
    first_three = dict(result["experiments"])
    assert len(first_three) == 3
    monkeypatch.setattr(workflow, "measure", original)
    result = run(store, "resume")
    assert result["state"] == "COMPLETED"
    assert all(result["experiments"][key] == value for key, value in first_three.items())
    assert "error" not in result


def test_api_reports_and_fixture_create(tmp_path):
    store = Store(tmp_path / "store")
    repo = store.root / "fixtures/stable"
    shas = fixture.fixture_repo(repo, "stable")
    config = read_config(ROOT / "configs/fixture.yaml").model_dump(mode="json")
    client = TestClient(app_for(store), base_url="http://localhost")
    response = client.post("/api/investigations", json={
        "repository": "stable", "good": shas[0], "bad": shas[-1], "config": config,
    })
    assert response.status_code == 202
    identifier = response.json()["id"]
    assert client.get(f"/api/investigations/{identifier}").json()["gate"]["decision"] == "PASS"
    assert client.get(f"/api/investigations/{identifier}/metrics").json()
    assert client.get(f"/api/investigations/{identifier}/commits").json()["decisions"] == []
    response = client.get(f"/api/investigations/{identifier}/report", params={"format": "md"})
    assert response.status_code == 200
    assert "## Reproduction" in response.text
    response = client.get(f"/api/investigations/{identifier}/report", params={"format": "json"})
    assert json.loads(response.text)["id"] == identifier
    assert client.get("/api/investigations/missing").status_code == 404
    assert client.get("/api/investigations", headers={"host": "foreign.example"}).status_code == 400
