"""Construct a reproducible commit history and run the full P0 workflow."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from regresslens.model import read_config
from regresslens.report import render
from regresslens.storage import Store
from regresslens.workflow import create, run, verify


def fixture_repo(path: Path, case: str):
    if path.exists():
        raise FileExistsError(path)
    path.mkdir(parents=True)

    def git(*args, date="2026-11-24T00:00:00+00:00"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        return subprocess.check_output(["git", "-C", str(path), "-c", "core.hooksPath=/dev/null",
                                        "-c", "user.name=RegressLens Fixture",
                                        "-c", "user.email=fixture@example.invalid", *args],
                                       text=True, env=env).strip()

    git("init", "-q")
    states = [
        ("Initialize scheduler fixture", {}),
        ("Record experiment metadata", {}),
        ("Add short prompt workload", {}),
        ("Document long context traffic", {}),
        ("Temporarily break the build", {"build_failure": True}),
        ("Restore build configuration", {}),
        ("Change scheduler admission policy", {
            "regression": {"delay": 0.31}, "noisy": {"noisy": True},
            "environment": {"environment_drift": True}, "stable": {"delay": 0.012},
        }[case]),
        ("Document throughput counters", None),
        ("Publish candidate configuration", None),
    ]
    shas = []
    for idx, (message, state) in enumerate(states):
        if state is not None:
            (path / "scheduler.json").write_text(json.dumps(state, indent=2) + "\n")
        if idx != 6:
            (path / "notes.txt").write_text(f"Fixture revision {idx}\n")
        git("add", ".")
        git("commit", "-qm", message, date=f"2026-11-24T00:0{idx}:00+00:00")
        shas.append(git("rev-parse", "HEAD"))
    return shas


def demo(dest: Path, case: str, config_path: Path):
    repo = dest / "repositories" / case
    shas = fixture_repo(repo, case)
    store = Store(dest / "store")
    inv = create(store, repo, shas[0], shas[-1], read_config(config_path), case)
    inv = run(store, inv["id"])
    if case == "regression":
        inv = verify(store, inv["id"], "RCA-001", shas[6], trusted=False)
    for format in ("html", "json", "md"):
        if format != "html" or (Path(__file__).parents[1] / "src/regresslens/web/index.html").exists():
            (dest / f"{case}.{format}").write_text(render(store, inv, format))
    print(f"{case}: {inv['gate']['decision']} / {inv['state']}")
    return inv, shas


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--out", type=Path, default=Path(".regresslens/demo"))
    cli.add_argument("--case", choices=["all", "regression", "stable", "noisy", "environment"], default="all")
    args = cli.parse_args()
    for name in ("regression", "stable", "noisy", "environment") if args.case == "all" else (args.case,):
        demo(args.out.resolve(), name, Path(__file__).parents[1] / "configs/fixture.yaml")
