#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def verify_runtime_backup(
    *,
    backup_dir: Path,
    restore_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    selected = _select_backup(backup_dir, stamp=stamp)
    cleanup = False
    if restore_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="automaticcosmetic-restore-check-")
        restore_root = Path(temp_dir.name)
        cleanup = True
    else:
        temp_dir = None
        restore_root = restore_dir
        restore_root.mkdir(parents=True, exist_ok=True)
    try:
        sqlite_restore_dir = restore_root / "sqlite"
        archive_restore_dir = restore_root / "runtime"
        sqlite_restore_dir.mkdir(parents=True, exist_ok=True)
        archive_restore_dir.mkdir(parents=True, exist_ok=True)

        sqlite_results = []
        for source in sorted(selected["sqlite_dir"].glob("*")):
            if not source.is_file():
                continue
            target = sqlite_restore_dir / source.name
            shutil.copy2(source, target)
            sqlite_results.append(_sqlite_integrity(target))

        archived_files = _safe_extract_tar(selected["archive_path"], archive_restore_dir)
        has_env = ".env" in archived_files
        has_totp = any(name.endswith("mfa_totp.json") for name in archived_files)
        ok = bool(sqlite_results) and all(row["ok"] for row in sqlite_results) and bool(archived_files)
        return {
            "ok": ok,
            "backup_dir": str(backup_dir),
            "stamp": selected["stamp"],
            "sqlite_dir": str(selected["sqlite_dir"]),
            "archive_path": str(selected["archive_path"]),
            "restore_dir": str(restore_root),
            "restore_dir_persistent": not cleanup,
            "sqlite": sqlite_results,
            "archived_files_count": len(archived_files),
            "archived_files_sample": archived_files[:20],
            "contains_env": has_env,
            "contains_mfa_totp": has_totp,
            "contains_sensitive_runtime_secrets": has_env or has_totp,
        }
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def _select_backup(backup_dir: Path, *, stamp: str | None) -> dict[str, Any]:
    if stamp:
        sqlite_dir = backup_dir / f"sqlite-{stamp}"
        archive_path = backup_dir / f"runtime-json-env-{stamp}.tar.gz"
        if not sqlite_dir.is_dir() or not archive_path.is_file():
            raise FileNotFoundError(f"Backup stamp not found under {backup_dir}: {stamp}")
        return {"stamp": stamp, "sqlite_dir": sqlite_dir, "archive_path": archive_path}

    candidates = []
    for sqlite_dir in backup_dir.glob("sqlite-*"):
        if not sqlite_dir.is_dir():
            continue
        current_stamp = sqlite_dir.name.removeprefix("sqlite-")
        archive_path = backup_dir / f"runtime-json-env-{current_stamp}.tar.gz"
        if archive_path.is_file():
            candidates.append((current_stamp, sqlite_dir, archive_path))
    if not candidates:
        raise FileNotFoundError(f"No paired sqlite/runtime-json-env backups found under {backup_dir}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected_stamp, sqlite_dir, archive_path = candidates[0]
    return {"stamp": selected_stamp, "sqlite_dir": sqlite_dir, "archive_path": archive_path}


def _sqlite_integrity(path: Path) -> dict[str, Any]:
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    message = str(result[0] if result else "")
    return {
        "path": str(path),
        "ok": message == "ok",
        "integrity_check": message,
    }


def _safe_extract_tar(archive_path: Path, target_dir: Path) -> list[str]:
    names: list[str] = []
    target_root = target_dir.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_target = (target_dir / member.name).resolve()
            if target_root not in (member_target, *member_target.parents):
                raise ValueError(f"Unsafe archive path: {member.name}")
            archive.extract(member, path=target_dir, filter="data")
            names.append(member.name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify AutomaticCosmetic runtime backup by restoring it outside live data.")
    parser.add_argument("--backup-dir", type=Path, default=Path("backups"))
    parser.add_argument("--restore-dir", type=Path)
    parser.add_argument("--stamp")
    args = parser.parse_args()
    try:
        result = verify_runtime_backup(backup_dir=args.backup_dir, restore_dir=args.restore_dir, stamp=args.stamp)
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
