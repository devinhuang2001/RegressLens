"""Versioned machine interface for callers that invoke RegressLens via argv."""
from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


SCHEMA_VERSION = 1
COMMANDS = {
    "capabilities": "Discover CLI operations and the output contract",
    "create": "Register a reportable investigation without starting a benchmark",
    "investigate": "Create, reproduce, compare, bisect and diagnose an investigation",
    "bisect": "Run the same workflow as investigate",
    "resume": "Resume an existing investigation or return its saved result",
    "benchmark": "Record experiments against a local OpenAI-compatible endpoint",
    "compare": "Compare two sets of benchmark experiments without launching a server",
    "list": "List persisted investigations",
    "status": "Read investigation state, regression gate and localization",
    "inspect": "Read metrics, commits, findings, runs, profiling, verification or configuration",
    "diagnose": "Read persisted root-cause hypotheses",
    "request-verification": "Record a manual verification request",
    "verify": "Execute an isolated revert experiment",
    "profile attach": "Import paired Chrome Trace or Speedscope artifacts",
    "profile capture": "Run an independent profiling replay in managed vLLM",
    "artifact list": "List the files persisted for an investigation",
    "artifact export": "Copy a raw experiment, trace, patch or other artifact to a new file",
    "report": "Export the complete investigation as HTML, Markdown or JSON",
    "serve": "Start the local Dashboard and API",
}


def product_version() -> str:
    try:
        return version("regresslens")
    except PackageNotFoundError:
        # Source checkouts can also run via python -m without an installation.
        import tomllib

        project = Path(__file__).parents[2] / "pyproject.toml"
        return tomllib.loads(project.read_text())["project"]["version"] if project.is_file() else "unknown"


def command_name(args) -> str:
    detail = getattr(args, "profile_command", None) or getattr(args, "artifact_command", None)
    return f"{args.command} {detail}" if detail else args.command


def capabilities() -> dict:
    return {
        "version": product_version(),
        "schema_version": SCHEMA_VERSION,
        "invocation": ["regresslens", "--json", "--store", "<directory>", "<command>", "..."],
        "commands": [{"argv": name.split(), "summary": summary,
                      "machine_output": name != "serve"} for name, summary in COMMANDS.items()],
        "output_fields": ["schema_version", "command", "ok", "result", "error"],
        "exit_codes": {
            "0": "Command completed. Read result.gate.decision for the regression outcome.",
            "2": "Argument, input or execution error. Read error.code and error.message.",
            "130": "Command interrupted.",
        },
        "notes": [
            "Place global options before the command. Use --help after any command to discover arguments.",
            "Human help, --version and the long-running serve process have their own output.",
            "Running repository code in a managed workflow requires explicit --trusted-repo.",
        ],
    }


def emit_machine(command: str | None, result=None, error=None):
    print(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": error is None,
        "result": result,
        "error": error,
    }, ensure_ascii=False, allow_nan=False))
