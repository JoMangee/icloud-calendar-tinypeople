#!/usr/bin/env python3
"""Rotate iCloud bridge read auth by adding a new key/salt to config.

This keeps existing keys in place by default, so you can overlap old and new
digests while you update clients. Pass --retire-old after you are done.

Usage:
  python rotate_bridge_key.py
  python rotate_bridge_key.py --key-id agent-new
  python rotate_bridge_key.py --config ~/.tinyPeople/conf/icloud-calendar/config.json
  python rotate_bridge_key.py --base-url https://your-bridge-host.example
  python rotate_bridge_key.py --retire-old agent-main
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CONFIG = Path.home() / ".tinyPeople" / "conf" / "icloud-calendar" / "config.json"


def _make_key_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"agent-{stamp}-{suffix}"


def _generate_secret(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def _load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_config(path: Path, config: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Add a new bridge read-auth key and print digests.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    parser.add_argument("--key-id", default="", help="Key ID to create; defaults to a generated ID")
    parser.add_argument("--base-url", default="", help="Optional bridge base URL to pass to generate_digests.py")
    parser.add_argument("--replace-existing", action="store_true", help="Overwrite an existing key_id entry")
    parser.add_argument(
        "--retire-old",
        action="append",
        default=[],
        metavar="KEY_ID",
        help="Remove a previously active key_id after the new digests are printed. May be repeated.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"Error: config not found at {config_path}", file=sys.stderr)
        return 1

    config = _load_config(config_path)
    bridge = config.setdefault("bridge", {})
    read_auth = bridge.setdefault("read_auth", {})
    keys = read_auth.setdefault("keys", {})

    key_id = args.key_id.strip() or _make_key_id()
    if key_id in keys and not args.replace_existing:
        print(f"Error: key_id already exists: {key_id} (use --replace-existing to overwrite)", file=sys.stderr)
        return 1

    salt = _generate_secret(32)
    key = _generate_secret(32)
    keys[key_id] = {
        "salt": salt,
        "key": key,
    }

    backup_path = config_path.with_name(f"{config_path.name}.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.bak")
    backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    _write_config(config_path, config)

    print(f"Updated config: {config_path}")
    print(f"Backup written:  {backup_path}")
    print(f"New key_id:      {key_id}")
    print(f"New salt:        {salt}")
    print(f"New key:         {key}")

    generate_digests = Path(__file__).with_name("generate_digests.py")
    if not generate_digests.exists():
        print(f"Error: generate_digests.py not found at {generate_digests}", file=sys.stderr)
        return 1

    cmd = [sys.executable, str(generate_digests)]
    if args.base_url.strip():
        cmd.extend(["--base-url", args.base_url.strip()])

    print()
    print("Generating digests:")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    retired = []
    for old_key_id in dict.fromkeys(args.retire_old):
        if old_key_id == key_id:
            print(f"Skipping retirement of new key_id: {old_key_id}")
            continue
        if old_key_id in keys:
            retired.append(old_key_id)
            keys.pop(old_key_id, None)
        else:
            print(f"Warning: retire-old key_id not found: {old_key_id}")

    if retired:
        _write_config(config_path, config)
        print()
        print("Retired key_ids:")
        for old_key_id in retired:
            print(f"  - {old_key_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())