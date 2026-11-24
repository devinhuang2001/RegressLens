from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .model import Config, fingerprint


class StepError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def safe_environment() -> dict:
    names = {
        "PATH", "LANG", "LC_ALL", "CUDA_VISIBLE_DEVICES", "CUDA_HOME",
        "LD_LIBRARY_PATH", "HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE",
        "TMPDIR", "VIRTUAL_ENV", "NVIDIA_VISIBLE_DEVICES", "NCCL_P2P_DISABLE",
    }
    return {key: value for key, value in os.environ.items() if key in names}


def stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def command(args: list[str], cwd: Path, timeout: float, log: Path, phase: str) -> None:
    with log.open("wb") as output:
        try:
            process = subprocess.Popen(args, cwd=cwd, env=safe_environment(), stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            raise StepError(phase, f"Could not start command. See {log.name}") from error
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise StepError(phase, f"Command timed out after {timeout:.1f}s. See {log.name}") from error
        finally:
            stop(process)
        if code:
            raise StepError(phase, f"Command exited with code {code}. See {log.name}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        capture_output=True, check=True, text=True, timeout=30,
    ).stdout.strip()


def revision(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def history(repo: Path, good: str, bad: str) -> list[str]:
    chain = git(repo, "rev-list", "--first-parent", "--max-count=10001", bad).splitlines()
    if good == bad or good not in chain or chain.index(good) >= 10000:
        raise ValueError("Good must precede Bad on the first-parent chain within 10,000 commits")
    return list(reversed(chain[:chain.index(good) + 1]))


@contextmanager
def worktree(repo: Path, commit: str):
    temp = Path(tempfile.mkdtemp(prefix="regresslens-worktree-"))
    path = temp / "source"
    try:
        git(repo, "worktree", "add", "--detach", str(path), commit)
        yield path
    finally:
        try:
            if path.exists():
                git(repo, "worktree", "remove", "--force", str(path))
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(req.full_url).netloc != urlsplit(newurl).netloc or urlsplit(newurl).scheme != "http":
            raise ValueError("Local service redirected outside the configured origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_local(url: str, timeout: float = 2, *, method: str = "GET") -> bytes:
    opener = build_opener(ProxyHandler({}), SameOriginRedirect())
    with opener.open(Request(url, method=method), timeout=timeout) as response:
        if urlsplit(response.url).hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("Local service redirected away from loopback")
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("Service reply exceeded the capture limit")
        return data


def target_available(target: str) -> None:
    port = urlsplit(target).port
    if port is None or not 1 <= port <= 65535:
        raise ValueError("Service target requires a valid port")
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as error:
            raise StepError("ERROR", "Service port is occupied. Choose a dedicated benchmark port.") from error


@contextmanager
def serve(config: Config, work: Path, artifact: Path, remaining, *, extra_args: list[str] | None = None):
    service = config.service
    if service is None:
        raise ValueError("Managed investigation requires a service configuration")
    remaining()
    target_available(service.target)
    with (artifact / "service.log").open("wb") as log:
        try:
            process = subprocess.Popen(service.command + (extra_args or []), cwd=work, env=safe_environment(), stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            raise StepError("ERROR", "Service command could not start") from error
        try:
            end = time.monotonic() + min(service.startup_seconds, remaining())
            while time.monotonic() < end and process.poll() is None:
                try:
                    read_local(service.target + "/health")
                except (OSError, URLError, ValueError):
                    time.sleep(0.25)
                else:
                    break
            else:
                raise StepError("ERROR", "Service failed readiness check. See service.log")
            yield service.target
        finally:
            stop(process)


def gpu_snapshot() -> list[dict]:
    keys = ["uuid", "name", "memory.total", "driver_version", "utilization.gpu",
            "temperature.gpu", "power.draw", "clocks.sm"]
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(keys),
                                 "--format=csv,noheader,nounits"], capture_output=True,
                                check=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    items = [dict(zip(keys, row.split(", "))) for row in result.stdout.splitlines() if row.strip()]
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is None:
        return items
    selected = []
    for part in visible.split(","):
        if part.isdecimal() and int(part) < len(items):
            selected.append(items[int(part)])
        else:
            selected.extend(item for item in items if item["uuid"].startswith(part) and part)
    return selected


def float_value(text: object) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if abs(value) < float("inf") else None


def environment(config: Config, root: Path, idle: list[dict], probe: dict | None,
                observed: list[dict], commit: str, dirty: bool) -> tuple[dict, list[str]]:
    if config.backend == "fixture":
        info = {"gpu": [], "kind": "fixture", "runtime": {"benchmark": "fixture-v1"}}
    else:
        from importlib.metadata import PackageNotFoundError, version
        import platform

        def package(name):
            try:
                return version(name)
            except PackageNotFoundError:
                return None

        runtime = {"python": platform.python_version(), "guidellm": package("guidellm"),
                   "torch": package("torch"), "vllm": package("vllm")}
        runtime.update(probe or {})
        info = {
            "gpu": [{key: gpu[key] for key in ("uuid", "name", "memory.total", "driver_version")}
                    for gpu in (observed or idle)],
            "platform": platform.platform(),
            "runtime": runtime,
            "dependency_lock": None,
        }
        if config.dependency_lock:
            import hashlib
            info["dependency_lock"] = hashlib.sha256((root / config.dependency_lock).read_bytes()).hexdigest()
    info.update({"model": config.model, "model_revision": config.model_revision,
                 "tokenizer": config.tokenizer, "tokenizer_revision": config.tokenizer_revision})
    # The engine version and SHA are the intended treatment. Library and CUDA drift invalidate.
    identity = json.loads(json.dumps(info))
    identity["runtime"].pop("vllm", None)
    info["fingerprint"] = fingerprint(identity)
    info.update({"prelaunch_gpu": idle, "gpu_before_measurement": observed, "commit": commit,
                 "worktree_dirty": dirty, "service": config.service.model_dump() if config.service else None})
    warnings = []
    if config.require_gpu and not info["gpu"]:
        warnings.append("Required NVIDIA GPU inventory unavailable")
    for gpu in idle:
        utilization = float_value(gpu.get("utilization.gpu"))
        temperature = float_value(gpu.get("temperature.gpu"))
        if utilization is not None and utilization > config.max_idle_gpu_utilization:
            warnings.append("GPU utilization exceeded the prelaunch limit")
        if temperature is not None and temperature > config.max_idle_gpu_temperature:
            warnings.append("GPU temperature exceeded the prelaunch limit")
    return info, sorted(set(warnings))


def probe_runtime(config: Config, work: Path, artifact: Path, remaining) -> dict | None:
    if not config.service or not config.service.probe:
        return None
    log = artifact / "runtime.json"
    command(config.service.probe, work, min(30, remaining()), log, "ERROR")
    try:
        value = json.loads(log.read_text())
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
    except ValueError as error:
        raise StepError("ERROR", "Runtime probe did not emit a JSON object") from error
    return value
