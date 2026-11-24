"""Measure a deliberate admission-limit change on real vLLM in a local history."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from regresslens.model import Config, Scenario, Service
from regresslens.report import render
from regresslens.storage import Store, write_json
from regresslens.workflow import create, run, verify

from history import make_history

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
TREATMENT = [8, 8, 8, 8, 1, 1, 1, 1, 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--engine-python", type=Path, required=True)
    parser.add_argument("--device", choices=["gpu", "cpu"], default="gpu")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--port", type=int, default=18010)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--tokenizer-path", type=Path)
    parser.add_argument("--skip-verification", action="store_true")
    parser.add_argument("--smoke-only", action="store_true", help="Compare endpoints and skip interior candidates")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    repo = root / "source"
    store = Store(root / "store")
    runtimes = {"gpu": "gpu-trial", "cpu": "cpu-trial"}
    identifier = runtimes[args.device]
    if args.resume:
        inv = store.get(identifier)
    else:
        shas = make_history(repo, "max_num_seqs", TREATMENT)
        launcher = root / "launch_vllm.py"
        launcher.write_text("""import json
import os
import sys
from pathlib import Path

port, model, revision, device, *extra = sys.argv[1:]
sequences = json.loads(Path("scheduler.json").read_text())["max_num_seqs"]
dtype = "half" if device == "gpu" else "float32"
if device == "cpu":
    os.environ["VLLM_CPU_KVCACHE_SPACE"] = "1"
    os.environ["VLLM_CPU_OMP_THREADS_BIND"] = "auto"
    os.environ["OMP_NUM_THREADS"] = "2"
command = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", model,
           "--served-model-name", "Qwen/Qwen2.5-0.5B-Instruct", "--revision", revision,
           "--host", "127.0.0.1", "--port", port, "--dtype", dtype, "--enforce-eager",
           "--max-model-len", "768", "--max-num-seqs", str(sequences),
           "--no-enable-prefix-caching", "--disable-log-requests", "--seed", "42"]
if device == "gpu":
    command += ["--gpu-memory-utilization", "0.68"]
os.execv(sys.executable, command + extra)
""")
        probe = [
            str(args.engine_python.absolute()), "-c",
            "import importlib.metadata as m, json, torch, platform; "
            "print(json.dumps({'python':platform.python_version(),'torch':m.version('torch'),"
            "'vllm':m.version('vllm'),'cuda':torch.version.cuda}))",
        ]
        config = Config(
            backend="guidellm", case_type="controlled_injection", model=MODEL, model_revision=REVISION,
            tokenizer=str(args.tokenizer_path.resolve()) if args.tokenizer_path else MODEL,
            tokenizer_revision=REVISION, require_gpu=args.device == "gpu", collect_prometheus=True,
            rounds=args.rounds, timeout_seconds=180, time_budget_seconds=4200, warmup=True,
            cooldown_seconds=1, max_commits=0 if args.smoke_only else 5,
            gate_scenario="concurrent",
            scenarios=[Scenario(id="concurrent", prompt_tokens=128, output_tokens=24,
                                concurrency=8, requests=24)],
            slo={"ttft_ms": 1500, "tpot_ms": 150},
            service=Service(command=[str(args.engine_python.absolute()), str(launcher), str(args.port),
                                     str(args.model_path.resolve()) if args.model_path else MODEL,
                                     REVISION, args.device],
                            probe=probe, target=f"http://127.0.0.1:{args.port}", startup_seconds=120),
        )
        versions = subprocess.check_output(probe, text=True)
        (root / "engine-runtime.json").write_text(versions)
        config.dependency_lock = str(root / "engine-runtime.json")
        inv = create(store, repo, shas[0], shas[-1], config, identifier)
        write_json(root / "trial.json", {"commits": shas, "treatment": TREATMENT, "model": MODEL,
                                        "revision": REVISION, "device": args.device})
    print(shlex.join(["regresslens", "--store", str(store.root), "resume",
                      "--investigation", identifier, "--trusted-repo"]), flush=True)
    inv = run(store, identifier, trusted=True)
    if (not args.skip_verification and (inv.get("localization") or {}).get("status") == "LOCALIZED"
            and any(item["id"] == "RCA-001" for item in inv["findings"])
            and not any(row["status"] == "SUPPORTED" for row in inv["verifications"])):
        inv = verify(store, identifier, "RCA-001", inv["localization"]["first_fail"], trusted=True)
    for kind in ("json", "md", "html"):
        (root / f"{identifier}.{kind}").write_text(render(store, inv, kind))
    print(json.dumps({"state": inv["state"], "gate": inv["gate"], "localization": inv["localization"],
                      "verification": inv["verifications"], "error": inv.get("error")}, indent=2))
    return 2 if inv["state"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
