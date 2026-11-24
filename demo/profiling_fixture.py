"""Generate an offline report with explicitly synthetic profiling artifacts."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from fixture import demo

from regresslens.profiling import attach
from regresslens.report import render
from regresslens.storage import Store, write_json


def make_trace(bad: bool):
    events = [
        {"ph": "M", "pid": 1, "tid": 11, "name": "thread_name", "args": {"name": "Scheduler"}},
        {"ph": "M", "pid": 1, "tid": 12, "name": "thread_name", "args": {"name": "Model runner"}},
        {"ph": "M", "pid": 0, "tid": 7, "name": "thread_name", "args": {"name": "GPU 0 · stream 7"}},
    ]

    def event(name, start, duration, thread, category):
        events.append({"ph": "X", "name": name, "ts": 1_000_000 + start * 1000, "dur": duration * 1000,
                       "pid": 0 if thread == 7 else 1, "tid": thread, "cat": category})

    queue = 52 if bad else 24
    event("request.enqueue_wait", 0, queue, 11, "vllm")
    event("Scheduler.step", queue, 21 if bad else 10, 11, "python_function")
    event("Scheduler.can_admit", queue + 1, 14 if bad else 5, 11, "python_function")
    event("BlockPool.acquire", queue + 2, 6 if bad else 3, 11, "python_function")
    event("ModelRunner.prefill", queue + 22 if bad else queue + 12, 20, 12, "python_function")
    event("aten::linear", queue + 25 if bad else queue + 15, 6, 12, "cpu_op")
    event("flash_attn_fwd", queue + 25 if bad else queue + 15, 7.2 if bad else 4.8, 7, "kernel")
    event("gemm_fp16", queue + 34 if bad else queue + 24, 4, 7, "kernel")
    decode = queue + 43 if bad else queue + 33
    for step in range(3):
        at = decode + 12 * step
        event("ModelRunner.decode", at, 8, 12, "python_function")
        event("paged_attention", at + 1, 4.2 if bad else 4.1, 7, "kernel")
        event("gemm_fp16", at + 5.4, 1.8, 7, "kernel")
    return {"traceEvents": events}


def make_stacks(bad: bool):
    labels = ["Engine.step", "Scheduler.step", "Scheduler.can_admit", "BlockPool.acquire",
              "ModelRunner.run", "prefill", "decode", "sample_tokens", "RequestOutput"]
    return {
        "$schema": "https://www.speedscope.app/file-format-schema.json",
        "shared": {"frames": [{"name": item} for item in labels]},
        "profiles": [{
            "type": "sampled", "name": "Synthetic CPU stacks", "unit": "milliseconds",
            "startValue": 0, "endValue": 100,
            "samples": [[0, 1, 2], [0, 1, 2, 3], [0, 4, 5], [0, 4, 6], [0, 4, 6, 7], [0, 8]],
            "weights": [26, 19, 18, 17, 11, 9] if bad else [10, 9, 28, 25, 15, 13],
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".regresslens/profiling-demo"))
    args = parser.parse_args()
    root = args.out.resolve()
    demo(root, "regression", Path(__file__).parents[1] / "configs/fixture.yaml")
    sources = root / "profiling-inputs"
    sources.mkdir()
    traces, stacks = {}, {}
    for side in ("good", "bad"):
        traces[side] = sources / f"{side}.pt.trace.json.gz"
        stacks[side] = sources / f"{side}.speedscope.json"
        with gzip.open(traces[side], "wt", encoding="utf-8") as file:
            json.dump(make_trace(side == "bad"), file)
        write_json(stacks[side], make_stacks(side == "bad"))
    store = Store(root / "store")
    inv = store.get("regression")
    attach(store, inv["id"], traces=traces, stacks=stacks, bad_commit=inv["localization"]["first_fail"],
           source="synthetic_fixture")
    for format in ("html", "json", "md"):
        (root / f"regression.{format}").write_text(render(store, store.get("regression"), format))


if __name__ == "__main__":
    main()
