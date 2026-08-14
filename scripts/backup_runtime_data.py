from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_SQLITE_GLOBS = ("*.sqlite", "*.sqlite3", "*.db")
DEFAULT_JSON_GLOBS = ("*.json", "*.jsonl")


def backup_runtime_data(
    *,
    data_dir: Path,
    output_dir: Path,
    env_path: Path,
    retention_days: int = 14,
    now: int | None = None,
    dry_run: bool = False,
) -> dict:
    now_ts = int(time.time()) if now is None else int(now)
    stamp = datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    sqlite_dir = output_dir / f"sqlite-{stamp}"
    archive_path = output_dir / f"runtime-json-env-{stamp}.tar.gz"
    copied_sqlite: list[str] = []
    archived_files: list[str] = []

    sqlite_files = sorted(_iter_files(data_dir, DEFAULT_SQLITE_GLOBS))
    json_files = sorted(_iter_files(data_dir, DEFAULT_JSON_GLOBS))
    if not dry_run:
        sqlite_dir.mkdir(parents=True, exist_ok=True)
    for source in sqlite_files:
        target = sqlite_dir / source.name
        if dry_run:
            copied_sqlite.append(str(target))
            continue
        _sqlite_backup(source, target)
        copied_sqlite.append(str(target))

    archive_sources = [(source, Path("data") / source.relative_to(data_dir)) for source in json_files]
    if env_path.exists():
        archive_sources.append((env_path, Path(env_path.name)))
    if dry_run:
        archived_files = [str(arcname) for _, arcname in archive_sources]
    else:
        with tarfile.open(archive_path, "w:gz") as archive:
            for source, arcname in archive_sources:
                archive.add(source, arcname=str(arcname), recursive=False)
                archived_files.append(str(arcname))
        os.chmod(archive_path, 0o600)

    removed = [] if dry_run else prune_old_backups(output_dir, retention_days=retention_days, now=now_ts)
    return {
        "ok": True,
        "dry_run": dry_run,
        "created_at": stamp,
        "data_dir": str(data_dir),
        "output_dir": str(output_dir),
        "sqlite_dir": str(sqlite_dir),
        "archive_path": str(archive_path),
        "sqlite_files": copied_sqlite,
        "archived_files": archived_files,
        "removed": removed,
        "retention_days": retention_days,
    }


def prune_old_backups(output_dir: Path, *, retention_days: int, now: int | None = None) -> list[str]:
    now_ts = int(time.time()) if now is None else int(now)
    cutoff = now_ts - max(1, int(retention_days or 14)) * 24 * 60 * 60
    removed: list[str] = []
    for path in output_dir.iterdir() if output_dir.exists() else []:
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if path.is_dir() and path.name.startswith("sqlite-"):
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.is_file() and path.name.startswith("runtime-json-env-") and path.suffixes[-2:] == [".tar", ".gz"]:
            path.unlink()
            removed.append(str(path))
    return removed


def _iter_files(root: Path, patterns: Iterable[str]) -> Iterable[Path]:
    if not root.exists():
        return []
    seen: set[Path] = set()
    result: list[Path] = []
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file() and path not in seen:
                seen.add(path)
                result.append(path)
    return result


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    os.chmod(target, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup AutomaticCosmetic runtime data.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    parser.add_argument("--env-path", type=Path, default=Path(".env"))
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = backup_runtime_data(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        env_path=args.env_path,
        retention_days=args.retention_days,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
