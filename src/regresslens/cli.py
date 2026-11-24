from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from .artifacts import export_artifact, list_artifacts
from .cli_contract import COMMANDS, capabilities, command_name, emit_machine, product_version
from .model import Experiment, read_config
from .profiling import attach, get_profile
from .report import render
from .runner import revision
from .statistics import compare_all
from .storage import Store, write_json
from .workflow import Budget, create, measure, request_verification, run, verify

INSPECTION_SECTIONS = ("metrics", "commits", "findings", "runs", "verification", "profiling", "config")


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser():
    root = Parser(prog="regresslens")
    root.add_argument("--store", type=Path, default=Path(".regresslens"))
    root.add_argument("--json", action="store_true", help="Emit the versioned machine response on stdout")
    root.add_argument("--version", action="version", version=f"%(prog)s {product_version()}")
    commands = root.add_subparsers(dest="command", required=True)

    def add(name):
        return commands.add_parser(name, help=COMMANDS[name])

    add("capabilities")
    add("list")
    for name in ("create", "investigate", "bisect"):
        sub = add(name)
        sub.add_argument("--repo", type=Path, required=True)
        sub.add_argument("--good", required=True)
        sub.add_argument("--bad", required=True)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--id")
        if name != "create":
            sub.add_argument("--trusted-repo", action="store_true")
    sub = add("resume")
    sub.add_argument("--investigation", required=True)
    sub.add_argument("--trusted-repo", action="store_true")
    sub = add("benchmark")
    sub.add_argument("--config", type=Path, required=True)
    sub.add_argument("--target", default="http://127.0.0.1:8000")
    sub.add_argument("--repo", type=Path)
    sub.add_argument("--commit", default="HEAD")
    sub.add_argument("--out", type=Path, required=True)
    sub = add("compare")
    sub.add_argument("--config", type=Path, required=True)
    sub.add_argument("--baseline", type=Path, required=True)
    sub.add_argument("--candidate", type=Path, required=True)
    sub = add("verify")
    sub.add_argument("--investigation", required=True)
    sub.add_argument("--hypothesis", required=True)
    sub.add_argument("--revert", required=True, help="Full SHA to revert in an isolated worktree")
    sub.add_argument("--trusted-repo", action="store_true")
    sub = add("request-verification")
    sub.add_argument("--investigation", required=True)
    sub.add_argument("--hypothesis", required=True)
    sub.add_argument("--reason", required=True)
    sub = commands.add_parser("profile", help="Attach or capture an independent profiling pair")
    profiles = sub.add_subparsers(dest="profile_command", required=True)
    imported = profiles.add_parser("attach", help=COMMANDS["profile attach"])
    imported.add_argument("--good-trace", type=Path)
    imported.add_argument("--bad-trace", type=Path)
    imported.add_argument("--good-stacks", type=Path)
    imported.add_argument("--bad-stacks", type=Path)
    captured = profiles.add_parser("capture", help=COMMANDS["profile capture"])
    captured.add_argument("--trusted-repo", action="store_true")
    captured.add_argument("--requests", type=int)
    captured.add_argument("--trace-index", type=int, default=0)
    for mode in (imported, captured):
        mode.add_argument("--investigation", required=True)
        mode.add_argument("--bad-commit")
        mode.add_argument("--scenario")
    for name in ("status", "inspect", "diagnose", "report"):
        sub = add(name)
        sub.add_argument("--investigation", required=True)
        if name == "inspect":
            sub.add_argument("--section", required=True, choices=INSPECTION_SECTIONS)
            sub.add_argument("--run-id", help="Return one run record when inspecting runs")
        if name == "report":
            sub.add_argument("--format", default="html", choices=("html", "md", "markdown", "json"))
            sub.add_argument("--out", type=Path)
    sub = commands.add_parser("artifact", help="Access the underlying evidence files")
    artifacts = sub.add_subparsers(dest="artifact_command", required=True)
    for mode in ("list", "export"):
        sub = artifacts.add_parser(mode, help=COMMANDS[f"artifact {mode}"])
        sub.add_argument("--investigation", required=True)
        if mode == "export":
            sub.add_argument("--path", required=True, help="Relative path returned by artifact list")
            sub.add_argument("--out", type=Path, required=True)
    sub = add("serve")
    sub.add_argument("--port", type=int, default=8765)
    return root


def output(args, result, *, human: str | None = None, error: dict | None = None):
    if args.json:
        emit_machine(command_name(args), result=result, error=error)
    else:
        print(human if human is not None else json.dumps(result, indent=2))
    return 2 if error else 0


def benchmark(args):
    cfg = read_config(args.config)
    if cfg.backend != "guidellm":
        raise ValueError("Use investigate for a controlled fixture")
    if not re.fullmatch(r"http://(?:localhost|127\.0\.0\.1):[0-9]+", args.target):
        raise ValueError("Configure a local OpenAI-compatible endpoint")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    cwd = (args.repo or Path.cwd()).resolve()
    commit = revision(cwd, args.commit) if args.repo else "unmanaged"
    budget = Budget(cfg.time_budget_seconds)
    runs = []
    for scenario in cfg.scenarios:
        for index in range(cfg.rounds):
            budget.cooldown(cfg.cooldown_seconds)
            artifact = out / "runs" / "manual" / "endpoint" / scenario.id / f"pair-{index:02d}"
            runs.append(measure(cfg, cwd, commit, scenario, index, "manual", artifact, budget,
                                external_target=args.target).model_dump(mode="json"))
            write_json(out / "experiments.json", runs)
    path = str(out / "experiments.json")
    return output(args, {"path": path, "experiments": len(runs)}, human=path)


def main(argv=None):
    tokens = list(sys.argv[1:] if argv is None else argv)
    root = parser()
    args = None
    try:
        args = root.parse_args(tokens)
        return execute(args)
    except (KeyError, ValueError, OSError, sqlite3.Error, subprocess.SubprocessError, RuntimeError) as error:
        if isinstance(error, UsageError):
            code = "usage_error"
        elif isinstance(error, (KeyError, FileNotFoundError)):
            code = "not_found"
        elif isinstance(error, FileExistsError):
            code = "already_exists"
        elif isinstance(error, ValueError):
            code = "invalid_input"
        else:
            code = "execution_error"
        message = str(error) if isinstance(error, (KeyError, ValueError, RuntimeError, FileNotFoundError)) else type(error).__name__
        if (args and args.json) or (args is None and "--json" in tokens):
            emit_machine(command_name(args) if args else None, error={"code": code, "message": message})
        else:
            if isinstance(error, UsageError):
                root.print_usage(sys.stderr)
            print(f"RegressLens: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if args and args.json:
            emit_machine(command_name(args), error={"code": "interrupted", "message": "Command interrupted"})
        return 130


def investigation_summary(inv):
    return {"id": inv["id"], "state": inv["state"], "gate": inv["gate"],
            "localization": inv["localization"], "error": inv.get("error")}


def run_response(args, inv):
    error = None
    if inv["state"] == "FAILED":
        error = {"code": "investigation_failed",
                 "message": inv.get("error") or "Investigation failed. Read result.gate and inspect the stored evidence."}
    return output(args, investigation_summary(inv), error=error)


def inspect(store, inv, section, run_id):
    if run_id is not None and section != "runs":
        raise ValueError("--run-id requires --section runs")
    if section == "commits":
        return {key: inv[key] for key in ("commits", "decisions", "localization")}
    if section == "runs":
        return inv["experiments"][run_id] if run_id else inv["experiments"]
    if section == "verification":
        return inv["verifications"]
    if section == "profiling":
        return get_profile(store, inv)
    return inv[section]


def execute(args):
    if args.command == "capabilities":
        return output(args, capabilities())
    if args.command == "benchmark":
        return benchmark(args)
    if args.command == "compare":
        config = read_config(args.config)
        a, b = ([Experiment.model_validate(row) for row in json.loads(path.read_text())]
                for path in [args.baseline, args.candidate])
        gate, metrics = compare_all(a, b, config)
        return output(args, {"gate": gate.model_dump(), "metrics": [m.model_dump() for m in metrics],
                             "note": "Pair indices and seeds are checked. Standalone callers control run order."})
    if args.command == "serve" and args.json:
        raise ValueError("Start serve without --json. It is a long-running server.")
    store = Store(args.store)
    if args.command in ("create", "investigate", "bisect"):
        config = read_config(args.config)
        if args.command != "create" and config.backend == "guidellm" and (not args.trusted_repo or not config.service):
            raise ValueError("Managed benchmark requires a service configuration and --trusted-repo")
        inv = create(store, args.repo, args.good, args.bad, config, args.id)
        if args.command == "create":
            return output(args, investigation_summary(inv))
        print(f"Investigation {inv['id']}", file=sys.stderr, flush=True)
        return run_response(args, run(store, inv["id"], trusted=args.trusted_repo))
    if args.command == "resume":
        return run_response(args, run(store, args.investigation, trusted=args.trusted_repo))
    if args.command == "list":
        fields = ("id", "state", "case_type", "updated_at")
        return output(args, [{key: inv[key] for key in fields} for inv in store.list()])
    if args.command == "status":
        inv = store.get(args.investigation)
        return output(args, {**investigation_summary(inv), "updated_at": inv["updated_at"],
                             "case_type": inv["case_type"], "recorded_runs": len(inv["experiments"]),
                             "has_profiling": bool(inv.get("profiling_manifest"))})
    if args.command == "inspect":
        inv = store.get(args.investigation)
        return output(args, inspect(store, inv, args.section, args.run_id))
    if args.command == "request-verification":
        return output(args, request_verification(store, args.investigation, args.hypothesis, args.reason))
    if args.command == "verify":
        inv = verify(store, args.investigation, args.hypothesis, args.revert, trusted=args.trusted_repo)
        return output(args, inv["verifications"][-1])
    if args.command == "profile":
        if args.profile_command == "attach":
            def pair(good, bad):
                if (good is None) != (bad is None):
                    raise ValueError("Provide both Good and Bad files for each profiling format")
                return {"good": good, "bad": bad} if good else None

            profile = attach(store, args.investigation, traces=pair(args.good_trace, args.bad_trace),
                             stacks=pair(args.good_stacks, args.bad_stacks),
                             bad_commit=args.bad_commit, scenario=args.scenario)
        else:
            from .profile_capture import capture

            profile = capture(store, args.investigation, trusted=args.trusted_repo,
                              bad_commit=args.bad_commit, scenario=args.scenario,
                              request_count=args.requests, trace_index=args.trace_index)
        return output(args, {key: profile[key] for key in ("commits", "scenario", "artifacts", "warnings")})
    if args.command == "artifact":
        if args.artifact_command == "list":
            return output(args, list_artifacts(store, args.investigation))
        return output(args, export_artifact(store, args.investigation, args.path, args.out))
    if args.command == "diagnose":
        return output(args, store.get(args.investigation)["findings"])
    if args.command == "report":
        content = render(store, store.get(args.investigation), args.format)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(content)
            if args.json:
                return output(args, {"format": args.format, "path": str(args.out.resolve())})
        elif args.json:
            return output(args, {"format": args.format,
                                 "content": json.loads(content) if args.format == "json" else content})
        else:
            print(content)
        return 0
    if args.command == "serve":
        import uvicorn
        from .api import app_for

        uvicorn.run(app_for(store), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
