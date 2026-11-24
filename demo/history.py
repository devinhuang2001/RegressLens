"""Local commit histories used by the controlled hardware and protocol cases."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def make_history(repo: Path, treatment: str, values: list):
    repo.mkdir(parents=True)

    def git(*args, generation=0):
        date = f"2026-11-24T00:{generation:02d}:00+00:00"
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo),
                                        "-c", "user.name=RegressLens Experiment",
                                        "-c", "user.email=experiment@example.invalid", *args],
                                       text=True, env=env).strip()

    git("init", "-q")
    shas = []
    old = None
    for number, value in enumerate(values):
        if value != old:
            (repo / "scheduler.json").write_text(json.dumps({treatment: value}, indent=2) + "\n")
        message = f"Record controlled revision {number}"
        if number and value != old:
            message = f"Change scheduler {treatment} from {old} to {value}"
        else:
            (repo / "revision.txt").write_text(f"Controlled revision {number}\n")
        git("add", ".")
        git("commit", "-qm", message, generation=number)
        shas.append(git("rev-parse", "HEAD"))
        old = value
    return shas
