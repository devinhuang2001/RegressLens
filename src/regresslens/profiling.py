"""Import read-only profiling evidence linked to an existing investigation."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

from .model import timestamp
from .storage import Store, write_json

MAX_BYTES = 64 * 1024 * 1024
TIMELINE_EVENTS = 1200
MAX_STACK_DEPTH = 128
GPU_CATEGORIES = {"kernel", "gpu_kernel", "cuda_kernel", "hip_kernel"}
TIME_SCALES = {"nanoseconds": .001, "microseconds": 1, "milliseconds": 1000, "seconds": 1e6}
Frame = tuple[str, str]
Stack = tuple[Frame, ...]


def number(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and abs(value) < 1e30 else None
    except OverflowError:
        return None


def label(value, fallback=""):
    return value[:500] if isinstance(value, str) and value else fallback


@dataclass
class Stacks:
    unit: str
    source: str
    weights: Counter[Stack] = field(default_factory=Counter)

    def add(self, stack: Stack, weight: float):
        if weight > 0 and stack:
            self.weights[stack] += weight


def read_document(path: Path) -> tuple[object, bytes, bool]:
    if not path.is_file():
        raise ValueError(f"Profiling file does not exist: {path.name}")
    with path.open("rb") as file:
        raw = file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{path.name} exceeds the 64 MiB input limit")
    compressed = raw.startswith(b"\x1f\x8b")
    try:
        if compressed:
            from io import BytesIO
            with gzip.GzipFile(fileobj=BytesIO(raw)) as file:
                data = file.read(MAX_BYTES + 1)
        else:
            data = raw
        if len(data) > MAX_BYTES:
            raise ValueError(f"{path.name} expands beyond 64 MiB")
        return json.loads(data), raw, compressed
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"Invalid profiling JSON: {path.name}") from error


def speedscope(payload: object) -> Stacks:
    if not isinstance(payload, dict):
        raise ValueError("Expected a Speedscope JSON document")
    shared = payload.get("shared")
    frames = shared.get("frames") if isinstance(shared, dict) else None
    profiles = payload.get("profiles")
    if not isinstance(frames, list) or not isinstance(profiles, list) or not profiles:
        raise ValueError("Speedscope requires shared frames and profiles")
    keys = []
    for frame in frames:
        if not isinstance(frame, dict) or not label(frame.get("name")):
            raise ValueError("Invalid Speedscope frame")
        keys.append((label(frame["name"]), label(frame.get("file"))))

    def frame_key(index):
        if type(index) is not int or not 0 <= index < len(keys):
            raise ValueError("Speedscope references an invalid frame")
        return keys[index]

    result = None
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("Invalid Speedscope profile")
        unit = label(profile.get("unit"), "none")
        scale = TIME_SCALES.get(unit, 1)
        normalized = "µs" if unit in TIME_SCALES else unit
        if normalized not in ("µs", "none"):
            raise ValueError("Use a CPU time or unitless sample profile")
        if result is None:
            result = Stacks(normalized, "speedscope")
        if result.unit != normalized:
            raise ValueError("Speedscope contains incompatible profile units")
        kind = profile.get("type")
        if kind == "sampled":
            samples, weights = profile.get("samples"), profile.get("weights")
            if not isinstance(samples, list) or not isinstance(weights, list) or len(samples) != len(weights):
                raise ValueError("Speedscope samples and weights must have matching lengths")
            for sample, raw in zip(samples, weights):
                weight = number(raw)
                if not isinstance(sample, list) or len(sample) > MAX_STACK_DEPTH or weight is None or weight < 0:
                    raise ValueError("Invalid Speedscope sample or weight")
                result.add(tuple(map(frame_key, sample)), weight * scale)
        elif kind == "evented":
            events = profile.get("events")
            previous = number(profile.get("startValue"))
            end = number(profile.get("endValue"))
            stack = []
            if not isinstance(events, list) or previous is None or end is None or end < previous:
                raise ValueError("Invalid Speedscope evented profile")
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("Invalid Speedscope event")
                when = number(event.get("at"))
                index = event.get("frame")
                frame_key(index)
                if when is None or not previous <= when <= end:
                    raise ValueError("Out-of-order Speedscope event")
                result.add(tuple(map(frame_key, stack)), (when - previous) * scale)
                if event.get("type") == "O" and len(stack) < MAX_STACK_DEPTH:
                    stack.append(index)
                elif event.get("type") == "C" and stack and stack[-1] == index:
                    stack.pop()
                else:
                    raise ValueError("Unbalanced Speedscope stack")
                previous = when
            if stack:
                raise ValueError("Speedscope evented profile has open frames")
        else:
            raise ValueError("Unsupported Speedscope profile type")
    if not result.weights:
        raise ValueError("Speedscope contains no positive stack weights")
    return result


def chrome_trace(payload: object):
    events = payload.get("traceEvents") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("Expected a Chrome Trace array or traceEvents document")
    labels = {}
    spans = []
    boundary = []
    for order, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        phase = event.get("ph")
        pid, tid = str(event.get("pid", ""))[:32], str(event.get("tid", ""))[:32]
        lane = (pid, tid)
        if phase == "M" and event.get("name") in ("process_name", "thread_name"):
            args = event.get("args")
            if isinstance(args, dict) and label(args.get("name")):
                labels[(pid, "" if event["name"] == "process_name" else tid)] = label(args["name"])
            continue
        at = number(event.get("ts"))
        if at is None:
            continue
        if phase == "X":
            duration = number(event.get("dur"))
            if duration is not None and duration > 0 and label(event.get("name")):
                spans.append((at, duration, lane, label(event["name"]), label(event.get("cat"))))
        elif phase in ("B", "E"):
            boundary.append((at, order, lane, phase, label(event.get("name")), label(event.get("cat"))))
    opened = defaultdict(list)
    for at, _, lane, phase, name, category in sorted(boundary):
        if phase == "B":
            opened[lane].append((at, name, category))
        elif opened[lane]:
            start, title, cat = opened[lane].pop()
            if at > start and title:
                spans.append((start, at - start, lane, title, cat))
    if not spans:
        raise ValueError("Trace contains no closed duration events")
    spans.sort(key=lambda row: (row[0], -row[1]))
    origin = spans[0][0]
    end = max(at + duration for at, duration, *_ in spans)
    by_kernel = defaultdict(lambda: {"count": 0, "total_us": 0.0})
    python_lanes = defaultdict(list)
    for at, duration, lane, title, category in spans:
        if GPU_CATEGORIES.intersection(part.strip() for part in category.lower().split(",")):
            by_kernel[title]["count"] += 1
            by_kernel[title]["total_us"] += duration
        if category.lower() == "python_function":
            python_lanes[lane].append((at, at + duration, (title, "")))

    cpu = Stacks("µs", "python_spans")
    skipped = 0
    # Kineto writes fractional microseconds at a large absolute clock offset.
    # Parent and child end times can round one float ULP in opposite directions.
    precision = max(1e-6, 2 * math.ulp(max(abs(origin), abs(end))))
    for calls in python_lanes.values():
        stack = []
        lane_weights = Stacks("µs", "python_spans")
        # Inclusive time is accumulated once via a call's exclusive time. Never
        # infer nesting for crossing intervals from different instrumentation.
        for start, stop, frame in calls:
            while stack and start >= stack[-1][0]:
                stack.pop()
            if stack and (stop > stack[-1][0] + precision or len(stack) >= MAX_STACK_DEPTH):
                skipped += 1
                break
            if stack:
                stop = min(stop, stack[-1][0])
                lane_weights.weights[tuple(item[1] for item in stack)] -= stop - start
            stack.append((stop, frame))
            lane_weights.weights[tuple(item[1] for item in stack)] += stop - start
        else:
            for path, weight in lane_weights.weights.items():
                cpu.add(path, weight)
    # Preserve representative long slices in the report. The original Chrome
    # Trace and the kernel totals remain complete.
    chosen = sorted(sorted(spans, key=lambda row: row[1], reverse=True)[:TIMELINE_EVENTS])
    visible = [{
        "at": round(at - origin, 3), "duration": round(duration, 3), "name": title, "category": category,
        "lane": f"{pid}/{tid}",
        "label": labels.get((pid, tid)) or labels.get((pid, "")) or f"PID {pid} · TID {tid}",
    } for at, duration, (pid, tid), title, category in chosen]
    return {"duration": end - origin, "count": len(spans), "events": visible,
            "skipped_stack_lanes": skipped}, dict(by_kernel), cpu


def flame_nodes(good: Stacks, bad: Stacks) -> dict:
    if (good.unit, good.source) != (bad.unit, bad.source):
        raise ValueError("Good and Bad stack profiles must share a source and weight unit")
    tree = [{"name": "All stacks", "file": "", "parent": -1, "good": 0.0, "bad": 0.0}]
    indexes = {}
    for side, stacks in (("good", good), ("bad", bad)):
        for path, weight in sorted(stacks.weights.items()):
            parent = 0
            tree[parent][side] += weight
            for name, file in path:
                key = (parent, name, file)
                if key not in indexes:
                    indexes[key] = len(tree)
                    tree.append({"name": name, "file": file, "parent": parent, "good": 0.0, "bad": 0.0})
                parent = indexes[key]
                tree[parent][side] += weight
    if len(tree) > 30_000:
        raise ValueError("Stack comparison exceeds 30,000 frames. Narrow the capture window.")
    return {"unit": good.unit, "source": good.source, "nodes": tree}


def kernel_rows(good: dict, bad: dict) -> list[dict]:
    result = [{"name": name, "good": good.get(name), "bad": bad.get(name)}
              for name in good.keys() | bad.keys()]
    return sorted(result, key=lambda row: (
        -abs((row["bad"] or {}).get("total_us", 0) - (row["good"] or {}).get("total_us", 0)), row["name"],
    ))


def selected_commits(inv: dict, bad_commit: str | None) -> dict:
    if bad_commit is None:
        bad = inv["bad"]
    else:
        matches = [row["sha"] for row in inv["commits"] if row["sha"].startswith(bad_commit)]
        if not bad_commit or len(matches) != 1 or matches[0] == inv["good"]:
            raise ValueError("Provide an unambiguous candidate commit from this investigation")
        bad = matches[0]
    return {"good": inv["good"], "bad": bad}


def attach(store: Store, identifier: str, *, traces: dict[str, Path] | None = None,
           stacks: dict[str, Path] | None = None, bad_commit: str | None = None,
           scenario: str | None = None, source: str = "imported", requests: int | None = None):
    if not traces and not stacks:
        raise ValueError("Provide matching Good and Bad traces or stack profiles")
    inv = store.get(identifier)
    commits = selected_commits(inv, bad_commit)
    scenarios = {row["id"] for row in inv["config"]["scenarios"]}
    workload = scenario or inv["config"]["gate_scenario"]
    if workload not in scenarios:
        raise ValueError("Profiling scenario is outside this investigation")
    record = {"schema_version": 1, "source": source, "scenario": workload, "commits": commits,
              "artifacts": {}, "traces": {}, "kernels": [], "kernel_comparable": False, "flame": None,
              "requests": requests, "warnings": []}
    with store.lock(identifier):
        inv = store.get(identifier)
        folder = store.path(identifier) / "profiles"
        folder.mkdir(exist_ok=True)
        kernels, traced, sampled = {}, {}, {}
        staged = []
        with tempfile.TemporaryDirectory(dir=folder, prefix=".stage-") as temp:
            for kind, sources in (("trace", traces), ("stacks", stacks)):
                if sources is None:
                    continue
                if set(sources) != {"good", "bad"}:
                    raise ValueError(f"Provide both Good and Bad {kind} files")
                for side, path in sources.items():
                    payload, raw, compressed = read_document(path)
                    if kind == "trace":
                        trace, kernels[side], traced[side] = chrome_trace(payload)
                        record["traces"][side] = trace
                        if trace["count"] > len(trace["events"]):
                            record["warnings"].append(f"{side.title()} timeline shows only the longest {TIMELINE_EVENTS:,} spans.")
                        if trace["skipped_stack_lanes"]:
                            record["warnings"].append(f"{side.title()} has Python lanes with ambiguous nesting. Those lanes were excluded from stack totals.")
                    else:
                        sampled[side] = speedscope(payload)
                    digest = hashlib.sha256(raw).hexdigest()
                    name = f"{side}-{kind}-{digest}.json" + (".gz" if compressed else "")
                    stage = Path(temp) / name
                    stage.write_bytes(raw)
                    staged.append(stage)
                    record["artifacts"][f"{side}-{kind}"] = {"name": name, "file": path.name, "sha256": digest}
                    del raw, payload
            if kernels:
                record["kernels"] = kernel_rows(kernels["good"], kernels["bad"])
                record["kernel_comparable"] = bool(kernels["good"] and kernels["bad"])
            weights = sampled or traced
            if weights and all(profile.weights for profile in weights.values()):
                record["flame"] = flame_nodes(weights["good"], weights["bad"])
            if traced and not record["flame"]:
                record["warnings"].append("Both captures need nested Python spans or matching Speedscope profiles for a flame graph.")
            # Names are content-addressed, so an interrupted import keeps the
            # previous active record and its raw files intact.
            for stage in staged:
                stage.replace(folder / stage.name)
            stamp = timestamp()
            identity = hashlib.sha256(json.dumps({
                key: record[key] for key in ("artifacts", "commits", "scenario", "source", "requests")
            }, sort_keys=True).encode()).hexdigest()
            manifest = folder / f"comparison-{identity}.json"
            record["attached_at"] = stamp
            write_json(manifest, record)
            inv["profiling_manifest"] = manifest.name
            store.save(inv)
    return record


def get_profile(store: Store, inv: dict):
    manifest = inv.get("profiling_manifest")
    if not manifest:
        return None
    if not re.fullmatch(r"comparison-[a-f0-9]{64}\.json", manifest):
        raise ValueError("Invalid profiling manifest")
    return json.loads((store.path(inv["id"]) / "profiles" / manifest).read_text())
