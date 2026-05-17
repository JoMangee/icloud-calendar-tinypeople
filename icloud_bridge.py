#!/usr/bin/env python3
"""
Read-only iCloud Calendar bridge — Flask/WSGI for cPanel Passenger + LiteSpeed.

Passenger entry point: passenger_wsgi.py (imports `app` as `application`)

Endpoints (all GET, read-only):
  /health
  /v1/calendars
  /v1/events/today
  /v1/events/upcoming?minutes=30
  /v1/events/list?days=7&limit=20

Auth params (every request):
  action   — one of: calendars, today, upcoming, list
  key_id   — identifies which key set to use (supports rotation)
  digest   — sha256("{salt}:{key}:{key_id}:{action}:{path}")
"""

import hashlib
import hmac
import os
import time

from flask import Flask, jsonify, request

import icloud_calendar as calendar

ACTION_PATHS = {
    "calendars": "/v1/calendars",
    "today": "/v1/events/today",
    "upcoming": "/v1/events/upcoming",
    "list": "/v1/events/list",
}

_rate_bucket: dict = {}


def _debug_flag_path() -> str:
    bridge = calendar.config.get("bridge", {}) or {}
    cfg_path = bridge.get("debug_flag_file", "")
    if cfg_path:
        return cfg_path
    return os.path.join(os.path.dirname(__file__), "tmp", "bridge-debug.on")


def _debug_enabled() -> bool:
    env = os.environ.get("ICLOUD_BRIDGE_DEBUG", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return os.path.exists(_debug_flag_path())


def _debug_log(message: str) -> None:
    if _debug_enabled():
        print(f"[bridge-debug][http] {message}", flush=True)


def _load_deploy_stamp() -> dict:
    """Read cPanel deploy metadata from .deploy-stamp.env if present."""
    stamp_path = os.path.join(os.path.dirname(__file__), ".deploy-stamp.env")
    info = {
        "revision": "unknown",
        "build": "unknown",
    }
    try:
        if not os.path.exists(stamp_path):
            return info
        with open(stamp_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k == "POD_APP_REVISION" and v:
                    info["revision"] = v
                elif k == "POD_APP_BUILD" and v:
                    info["build"] = v
    except OSError:
        return info
    return info


def _bridge_cfg() -> dict:
    bridge = calendar.config.get("bridge", {})
    rl = bridge.get("rate_limit", {})
    return {
        "keys": bridge.get("read_auth", {}).get("keys", {}),
        "rate_limit": {
            "enabled":        bool(rl.get("enabled", True)),
            "window_seconds": int(rl.get("window_seconds", 60)),
            "max_requests":   int(rl.get("max_requests", 120)),
        },
    }

# Load once at startup
_cfg = _bridge_cfg()
_deploy = _load_deploy_stamp()


def _compute_digest(salt: str, key: str, key_id: str, action: str, path: str) -> str:
    payload = f"{salt}:{key}:{key_id}:{action}:{path}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_rate_limited(client_ip: str) -> bool:
    rl = _cfg["rate_limit"]
    if not rl["enabled"]:
        return False
    now    = time.time()
    window = max(rl["window_seconds"], 1)
    cap    = max(rl["max_requests"], 1)
    recent = [t for t in _rate_bucket.get(client_ip, []) if now - t <= window]
    if len(recent) >= cap:
        _rate_bucket[client_ip] = recent
        return True
    recent.append(now)
    _rate_bucket[client_ip] = recent
    return False


def _validate_auth(path: str) -> tuple:
    action = (request.args.get("action", "") or "").strip()
    key_id = (request.args.get("key_id", "") or "").strip()
    digest = (request.args.get("digest", "") or "").strip().lower()

    if not action or not key_id or not digest:
        return False, "Missing action, key_id, or digest"

    if ACTION_PATHS.get(action) != path:
        return False, "Action does not match endpoint"

    key_cfg = _cfg["keys"].get(key_id, {})
    salt    = key_cfg.get("salt", "")
    key     = key_cfg.get("key",  "")
    if not salt or not key:
        return False, "Unknown key_id"

    expected = _compute_digest(salt, key, key_id, action, path)
    if not hmac.compare_digest(expected, digest):
        return False, "Forbidden"

    return True, ""


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


def _parse_int(raw, default, lo, hi):
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def _guard(path: str):
    """Run rate-limit + auth checks; return error response tuple or None."""
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    _debug_log(f"request path={path} ip={client_ip} query={request.query_string.decode('utf-8', 'ignore')}")
    if _is_rate_limited(client_ip):
        _debug_log(f"rate_limited path={path} ip={client_ip}")
        return jsonify({"ok": False, "error": "Too many requests"}), 429
    ok, err = _validate_auth(path)
    if not ok:
        _debug_log(f"auth_failed path={path} error={err}")
        return jsonify({"ok": False, "error": err}), 403
    return None


@app.route("/health")
def health():
    response = {
        "ok": True,
        "service": "icloud-calendar-bridge",
        "revision": _deploy["revision"],
        "build": _deploy["build"],
        "credentials_set": bool(getattr(calendar, "apple_id", "") and getattr(calendar, "app_password", "")),
        "calendar_count": len(getattr(calendar, "CALENDARS", {}) or {}),
    }
    if _debug_enabled():
        response["debug_flag_path"] = _debug_flag_path()
    return jsonify(response)


@app.route("/v1/calendars")
def route_calendars():
    err = _guard("/v1/calendars")
    if err:
        return err
    data = calendar.list_calendars()
    _debug_log(f"response path=/v1/calendars calendar_count={len(data or {})}")
    return jsonify({"ok": True, "data": data})


@app.route("/v1/events/today")
def route_today():
    err = _guard("/v1/events/today")
    if err:
        return err
    data = calendar.get_today_events()
    _debug_log(f"response path=/v1/events/today count={len((data or {}).get('today', []))}")
    return jsonify({"ok": True, "data": data})


@app.route("/v1/events/upcoming")
def route_upcoming():
    err = _guard("/v1/events/upcoming")
    if err:
        return err
    minutes = _parse_int(request.args.get("minutes"), 30, 1, 1440)
    data = calendar.get_upcoming_events(minutes=minutes)
    _debug_log(f"response path=/v1/events/upcoming minutes={minutes} count={len((data or {}).get('upcoming', []))}")
    return jsonify({"ok": True, "data": data})


@app.route("/v1/events/list")
def route_list():
    err = _guard("/v1/events/list")
    if err:
        return err
    days  = _parse_int(request.args.get("days"),  7,  1, 60)
    limit = _parse_int(request.args.get("limit"), 20, 1, 200)
    data = calendar.get_events_list(days=days, limit=limit)
    _debug_log(f"response path=/v1/events/list days={days} limit={limit} count={len((data or {}).get('events', []))}")
    return jsonify({"ok": True, "data": data})


@app.errorhandler(404)
def not_found(_):
    return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405


# ---------------------------------------------------------------------------
# Direct run (local dev only — not used by Passenger)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bridge = calendar.config.get("bridge", {})
    host = bridge.get("host", "127.0.0.1")
    port = int(bridge.get("port", 8088))
    app.run(host=host, port=port, debug=False)
