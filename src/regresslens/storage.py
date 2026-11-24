from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / "index.sqlite3", timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", identifier):
            raise ValueError("Invalid investigation ID")
        return self.root / identifier

    def save(self, investigation: dict, *, create: bool = False):
        identifier = investigation["id"]
        self.path(identifier)
        with self.connection() as db:
            sql = ("INSERT INTO investigations (id, body) VALUES (?, ?)" if create
                   else "UPDATE investigations SET body=? WHERE id=?")
            body = json.dumps(investigation, allow_nan=False)
            db.execute(sql, (identifier, body) if create else (body, identifier))

    def get(self, identifier: str) -> dict:
        self.path(identifier)
        with self.connection() as db:
            row = db.execute("SELECT body FROM investigations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row[0])

    def list(self) -> list[dict]:
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM investigations ORDER BY rowid DESC")]

    @contextmanager
    def lock(self, identifier: str):
        path = self.path(identifier)
        path.mkdir(exist_ok=True)
        with (path / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Investigation is already running") from error
            yield
