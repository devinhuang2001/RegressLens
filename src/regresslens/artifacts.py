"""Enumerate and export files owned by an investigation."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from .storage import Store


def artifact_root(store: Store, identifier: str) -> Path:
    root = store.path(identifier).resolve()
    if not root.is_relative_to(store.root) or root == store.root:
        raise ValueError("Investigation artifact directory is outside the store")
    return root


def artifact_path(store: Store, identifier: str, relative: str) -> Path:
    root = artifact_root(store, identifier)
    name = Path(relative)
    if name.is_absolute() or not name.parts or any(part.startswith(".") for part in name.parts):
        raise ValueError("Supply a visible artifact path relative to the investigation")
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact path escapes the investigation")
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path


def list_artifacts(store: Store, identifier: str) -> list[dict]:
    store.get(identifier)
    root = artifact_root(store, identifier)
    result = []
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if not file.is_file() or not file.resolve().is_relative_to(root):
            continue
        try:
            result.append({"path": relative.as_posix(), "size_bytes": file.stat().st_size})
        except FileNotFoundError:
            # A writer may atomically replace or remove an intermediate file.
            continue
    return result


def export_artifact(store: Store, identifier: str, relative: str, output: Path) -> dict:
    store.get(identifier)
    source = artifact_path(store, identifier, relative)
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Create exclusively via a same-directory hard link after the complete
    # copy, leaving preexisting exports and the source intact.
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.") as temp:
        with source.open("rb") as reader:
            shutil.copyfileobj(reader, temp)
        size = temp.tell()
        temp.flush()
        output.hardlink_to(temp.name)
    return {"source": Path(relative).as_posix(), "path": str(output), "size_bytes": size}
