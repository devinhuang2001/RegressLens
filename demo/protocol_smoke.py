"""Run the actual GuideLLM CLI against its local OpenAI-compatible mock."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from regresslens.model import Config, Scenario, Service
from regresslens.report import render
from regresslens.storage import Store
from regresslens.workflow import create, run

from history import make_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--revision", default="607a30d783dfa663caf39e06633721c8d4cfcd7e")
    parser.add_argument("--port", type=int, default=18011)
    options = parser.parse_args()
    root = options.out.resolve()
    shas = make_history(root / "source", "ttft_ms", [35, 120])
    launcher = root / "start_mock.py"
    launcher.write_text("""import json
import os
import sys
from pathlib import Path

value = json.loads(Path("scheduler.json").read_text())["ttft_ms"]
os.execvp("guidellm", ["guidellm", "mock-server", "--model", "regresslens-mock",
                       "--port", sys.argv[1], "--ttft-ms", str(value),
                       "--itl-ms", "3", "--output-tokens", "8"])
""")
    config = Config(
        case_type="local_smoke", model="regresslens-mock", model_revision="guidellm-mock-0.8.1",
        tokenizer=options.tokenizer, tokenizer_revision=options.revision,
        rounds=3, require_gpu=False, collect_prometheus=False, cooldown_seconds=0,
        gate_scenario="smoke", scenarios=[Scenario(id="smoke", prompt_tokens=32,
                                                   output_tokens=8, concurrency=1, requests=6)],
        slo={"ttft_ms": 200, "tpot_ms": 100},
        service=Service(command=[sys.executable, str(launcher), str(options.port)],
                        target=f"http://127.0.0.1:{options.port}", startup_seconds=30),
    )
    store = Store(root / "store")
    inv = create(store, root / "source", shas[0], shas[-1], config, "protocol-smoke")
    inv = run(store, inv["id"], trusted=True)
    for kind in ("json", "md", "html"):
        (root / f"protocol-smoke.{kind}").write_text(render(store, inv, kind))
    print(json.dumps({"gate": inv["gate"], "state": inv["state"], "error": inv.get("error")}, indent=2))
    return 0 if inv["state"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
