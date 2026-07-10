#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".env"
ACCESS_KEY = "ALLOWED_TELEGRAM_USERNAMES"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_username(raw: str) -> str:
    username = raw.strip().lstrip("@")
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Bad Telegram username: {raw!r}")
    return username


def split_usernames(value: str) -> list[str]:
    return [part.strip().lstrip("@") for part in value.split(",") if part.strip()]


def parse_env_value(line: str) -> str:
    _, value = line.split("=", 1)
    return value.strip().strip('"').strip("'")


def load_access(env_path: Path) -> tuple[list[str], list[str], int | None]:
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == ACCESS_KEY:
            return lines, split_usernames(parse_env_value(stripped)), index
    return lines, [], None


def save_access(env_path: Path, lines: list[str], index: int | None, usernames: list[str]) -> None:
    line = f"{ACCESS_KEY}={','.join(usernames)}"
    if index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(line)
    else:
        lines[index] = line
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_user(usernames: list[str], username: str) -> bool:
    existing = {item.lower() for item in usernames}
    if username.lower() in existing:
        return False
    usernames.append(username)
    return True


def remove_user(usernames: list[str], username: str) -> bool:
    before = len(usernames)
    usernames[:] = [item for item in usernames if item.lower() != username.lower()]
    return len(usernames) != before


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Telegram users allowed to use Codex bot.")
    parser.add_argument("--env", default=str(DEFAULT_ENV_PATH), help="Path to .env file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Show allowed Telegram usernames.")

    add_parser = subparsers.add_parser("add", help="Add allowed Telegram username.")
    add_parser.add_argument("username")

    remove_parser = subparsers.add_parser("remove", help="Remove allowed Telegram username.")
    remove_parser.add_argument("username")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_path = Path(args.env).expanduser().resolve()
    lines, usernames, index = load_access(env_path)

    if args.command == "list":
        for username in usernames:
            print(f"@{username}")
        return 0

    username = normalize_username(args.username)
    if args.command == "add":
        changed = add_user(usernames, username)
        save_access(env_path, lines, index, usernames)
        print(("added" if changed else "already allowed") + f": @{username}")
        return 0

    if args.command == "remove":
        changed = remove_user(usernames, username)
        save_access(env_path, lines, index, usernames)
        print(("removed" if changed else "not found") + f": @{username}")
        return 0

    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
