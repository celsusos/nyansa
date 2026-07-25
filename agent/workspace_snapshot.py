from __future__ import annotations
import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from tools.builtin.file_filter import ExclusionFilter

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_WORKSPACE_FILES = 20_000

SNAPSHOT_VERSION = 1


@dataclass
class RestoreSummary:
    reverted: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return path == base


def capture_files(
    paths: Iterable[str | Path],
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    total = 0

    for raw in sorted({str(p) for p in paths}):
        resolved = Path(raw)
        entry: dict[str, Any] = {"path": str(resolved)}

        if not resolved.exists():
            entry["existed"] = False
            entries.append(entry)
            continue

        entry["existed"] = True

        if resolved.is_dir():
            entry["skipped"] = "directory"
            logger.warning("workspace snapshot skipped directory %s", resolved)
            entries.append(entry)
            continue

        try:
            data = resolved.read_bytes()
        except OSError as error:
            entry["skipped"] = f"read-error: {error}"
            logger.warning("workspace snapshot could not read %s: %s", resolved, error)
            entries.append(entry)
            continue

        size = len(data)
        entry["size"] = size

        if size > max_file_bytes:
            entry["skipped"] = "file-too-large"
            logger.warning(
                "workspace snapshot skipped oversized file %s (%d bytes)",
                resolved,
                size,
            )
            entries.append(entry)
            continue

        if total + size > max_total_bytes:
            entry["skipped"] = "total-limit"
            logger.warning(
                "workspace snapshot dropped %s: total snapshot limit reached", resolved
            )
            entries.append(entry)
            continue

        total += size
        entry["content"] = base64.b64encode(data).decode("ascii")
        entries.append(entry)

    return {"version": SNAPSHOT_VERSION, "files": entries}


def _workspace_files(base: Path) -> tuple[list[Path], bool]:
    exclusions = ExclusionFilter(base)
    files: list[Path] = []
    capped = False

    for path in base.rglob("*"):
        if len(files) >= MAX_WORKSPACE_FILES:
            capped = True
            break
        if not path.is_file():
            continue
        if exclusions.is_excluded(path, is_dir=False):
            continue
        files.append(path)

    return files, capped


def capture_workspace(
    cwd: str | Path,
    extra_paths: Iterable[str | Path] = (),
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Snapshot every non-excluded file in the workspace so any change is reversible.

    Vendored, build and .gitignored paths are skipped. `extra_paths` (e.g. tracked
    edits) are always included even if they would otherwise be excluded.
    """
    base = Path(cwd).resolve()
    files, capped = _workspace_files(base)

    if capped:
        logger.warning(
            "workspace snapshot stopped at %d files; snapshot is incomplete",
            MAX_WORKSPACE_FILES,
        )

    all_paths = set(files) | {Path(p) for p in extra_paths}
    snapshot = capture_files(all_paths, max_file_bytes, max_total_bytes)
    snapshot["full"] = True
    snapshot["root"] = str(base)
    snapshot["truncated"] = capped

    return snapshot


def _prune_created_files(snapshot: dict[str, Any], base: Path, summary: RestoreSummary) -> None:
    root_raw = snapshot.get("root")
    if not root_raw:
        return

    root = Path(root_raw).resolve()
    if not _within(root, base):
        return

    known = set()
    for entry in snapshot.get("files", []):
        try:
            known.add(Path(entry.get("path", "")).resolve())
        except OSError:
            continue

    current, _ = _workspace_files(root)

    for path in current:
        if path.resolve() in known:
            continue
        try:
            path.unlink()
            summary.deleted.append(str(path))
        except OSError as error:
            summary.skipped.append((str(path), f"delete-error: {error}"))
            logger.warning("could not delete %s: %s", path, error)


def restore_files(snapshot: dict[str, Any], cwd: str | Path) -> RestoreSummary:
    summary = RestoreSummary()
    base = Path(cwd).resolve()

    for entry in snapshot.get("files", []):
        raw_path = entry.get("path", "")
        try:
            resolved = Path(raw_path).resolve()
        except OSError:
            resolved = Path(raw_path)

        if not _within(resolved, base):
            summary.skipped.append((raw_path, "outside-cwd"))
            logger.warning("refusing to restore path outside cwd: %s", raw_path)
            continue

        if entry.get("skipped"):
            summary.skipped.append((raw_path, entry["skipped"]))
            continue

        if not entry.get("existed", False):
            if resolved.is_dir():
                summary.skipped.append((raw_path, "is-directory"))
                continue
            if resolved.exists():
                try:
                    resolved.unlink()
                    summary.deleted.append(raw_path)
                except OSError as error:
                    summary.skipped.append((raw_path, f"delete-error: {error}"))
                    logger.warning("could not delete %s: %s", resolved, error)
            continue

        content = entry.get("content")
        if content is None:
            summary.skipped.append((raw_path, "no-content"))
            continue

        if resolved.is_dir():
            summary.skipped.append((raw_path, "is-directory"))
            continue

        try:
            data = base64.b64decode(content)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
            summary.reverted.append(raw_path)
        except (OSError, ValueError) as error:
            summary.skipped.append((raw_path, f"write-error: {error}"))
            logger.warning("could not restore %s: %s", resolved, error)

    if snapshot.get("full"):
        _prune_created_files(snapshot, base, summary)

    return summary
