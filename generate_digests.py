#!/usr/bin/env python3
"""
Generate precomputed digests for all bridge actions.

Run once after setting salt/key in config, then hardcode the
printed digests into your trigger URLs. Re-run if you rotate keys.

Usage:
    python3 generate_digests.py --base-url https://your-bridge-host.example
    ICLOUD_BRIDGE_BASE_URL=https://your-bridge-host.example python3 generate_digests.py
"""

import argparse
import hashlib
import json
import os
import sys

CONFIG_FILE = os.path.expanduser("~/.tinyPeople/conf/icloud-calendar/config.json")

ACTION_PATHS = {
    "calendars": "/v1/calendars",
    "today":     "/v1/events/today",
    "upcoming":  "/v1/events/upcoming",
    "list":      "/v1/events/list",
}


def resolve_base_url(cli_base_url: str, config: dict) -> str:
    if cli_base_url:
        return cli_base_url.rstrip("/")

    env_base_url = os.environ.get("ICLOUD_BRIDGE_BASE_URL", "").strip()
    if env_base_url:
        return env_base_url.rstrip("/")

    cfg_base_url = (
        config.get("bridge", {})
        .get("public_base_url", "")
        .strip()
    )
    if cfg_base_url:
        return cfg_base_url.rstrip("/")

    return ""


def compute_digest(salt: str, key: str, key_id: str, action: str, path: str) -> str:
    payload = f"{salt}:{key}:{key_id}:{action}:{path}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Generate bridge digests for all actions.")
    parser.add_argument(
        "--base-url",
        default="",
        help="Base URL of the bridge (or use ICLOUD_BRIDGE_BASE_URL / config bridge.public_base_url)",
    )
    args = parser.parse_args()
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Config not found at {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    base_url = resolve_base_url(args.base_url, config)
    if not base_url:
        print(
            "Error: Missing base URL. Set one of: --base-url, ICLOUD_BRIDGE_BASE_URL, or bridge.public_base_url in config.",
            file=sys.stderr,
        )
        sys.exit(1)

    keys = config.get("bridge", {}).get("read_auth", {}).get("keys", {})
    if not keys:
        print("Error: No keys found in bridge.read_auth.keys", file=sys.stderr)
        sys.exit(1)

    for key_id, key_cfg in keys.items():
        salt = key_cfg.get("salt", "")
        key  = key_cfg.get("key",  "")
        if not salt or not key or "replace-with" in salt or "replace-with" in key:
            print(f"[{key_id}] Skipped — salt/key not set (still placeholder)")
            continue

        print(f"\n=== key_id: {key_id} ===")
        for action, path in ACTION_PATHS.items():
            digest = compute_digest(salt, key, key_id, action, path)
            url    = f"{base_url}{path}?action={action}&key_id={key_id}&digest={digest}"
            print(f"\n  {action}")
            print(f"    digest: {digest}")
            print(f"    url:    {url}")

    print()


if __name__ == "__main__":
    main()
