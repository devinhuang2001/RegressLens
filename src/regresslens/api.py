from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from pathlib import Path
from subprocess import SubprocessError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .artifacts import artifact_path
from .model import Config, Strict
from .profiling import get_profile
from .report import WEB, render, report_data
from .storage import Store
from .workflow import create, request_verification, run


class FixtureRequest(Strict):
    repository: str = Field(min_length=1)
    good: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    bad: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    config: Config


class Verification(Strict):
    hypothesis: str = Field(pattern=r"^RCA-[0-9]{3}$")
    reason: str = Field(min_length=1, max_length=2000)


def app_for(store: Store) -> FastAPI:
    app = FastAPI(title="RegressLens Local API", version="0.2.1")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    def load(identifier):
        try:
            return store.get(identifier)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, detail={"code": "INVESTIGATION_NOT_FOUND"}) from error

    @app.get("/api/investigations")
    def list_investigations():
        return [{key: inv[key] for key in ("id", "state", "case_type", "updated_at")}
                for inv in store.list()]

    @app.post("/api/investigations", status_code=202)
    def create_investigation(body: FixtureRequest, tasks: BackgroundTasks):
        # API requests never launch user-supplied service or build commands.
        if body.config.backend != "fixture" or body.config.service is not None:
            raise HTTPException(403, detail={"code": "MANAGED_RUN_REQUIRES_LOCAL_CLI"})
        fixture_root = (store.root / "fixtures").resolve()
        repo = (fixture_root / body.repository).resolve()
        if repo == fixture_root or not repo.is_relative_to(fixture_root) or not (repo / ".git").is_dir():
            raise HTTPException(422, detail={"code": "INVALID_FIXTURE_REPOSITORY"})
        try:
            inv = create(store, repo, body.good, body.bad, body.config)
        except (ValueError, OSError, SubprocessError) as error:
            raise HTTPException(422, detail={"code": "INVALID_COMMIT_RANGE"}) from error
        tasks.add_task(run, store, inv["id"])
        return {"id": inv["id"], "state": inv["state"]}

    @app.get("/api/investigations/{identifier}")
    def get_investigation(identifier: str):
        return report_data(store, load(identifier))

    @app.get("/api/investigations/{identifier}/metrics")
    def get_metrics(identifier: str):
        return load(identifier)["metrics"]

    @app.get("/api/investigations/{identifier}/commits")
    def get_commits(identifier: str):
        inv = load(identifier)
        return {"commits": inv["commits"], "decisions": inv["decisions"], "localization": inv["localization"]}

    @app.get("/api/investigations/{identifier}/findings")
    def get_findings(identifier: str):
        return load(identifier)["findings"]

    @app.post("/api/investigations/{identifier}/verify", status_code=202)
    def create_verification_request(identifier: str, body: Verification):
        load(identifier)
        try:
            request_verification(store, identifier, body.hypothesis, body.reason)
        except RuntimeError as error:
            raise HTTPException(409, detail={"code": "INVESTIGATION_BUSY"}) from error
        except ValueError as error:
            raise HTTPException(422, detail={"code": "UNKNOWN_HYPOTHESIS"}) from error
        return {"status": "REQUESTED"}

    @app.get("/api/investigations/{identifier}/profiles/{kind}")
    def get_profile_artifact(identifier: str, kind: str):
        try:
            profile = get_profile(store, load(identifier))
        except (OSError, ValueError) as error:
            raise HTTPException(404, detail={"code": "PROFILE_MANIFEST_NOT_FOUND"}) from error
        artifact = profile["artifacts"].get(kind) if profile else None
        if not artifact:
            raise HTTPException(404, detail={"code": "PROFILE_ARTIFACT_NOT_FOUND"})
        try:
            path = artifact_path(store, identifier, f"profiles/{artifact['name']}")
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(404, detail={"code": "PROFILE_ARTIFACT_NOT_FOUND"}) from error
        return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/investigations/{identifier}/report")
    def get_report(identifier: str, format: str = "html"):
        inv = load(identifier)
        types = {"html": "text/html", "md": "text/markdown", "markdown": "text/markdown",
                 "json": "application/json"}
        if format not in types:
            raise HTTPException(422, detail={"code": "UNKNOWN_FORMAT"})
        return Response(render(store, inv, format), media_type=types[format],
                        headers={"Content-Disposition": f'attachment; filename="regresslens-{identifier}.{format}"'})

    if (WEB / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB / "index.html")
    return app
