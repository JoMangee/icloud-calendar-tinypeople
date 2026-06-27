#!/usr/bin/env python3
"""
iCloud Calendar access script
Usage:
  python3 icloud_calendar.py list        # List all events (next 7 days)
  python3 icloud_calendar.py upcoming   # Get events in the next 30 minutes
  python3 icloud_calendar.py today      # Get today's events
  python3 icloud_calendar.py calendars   # Get calendar list (including IDs)
  python3 icloud_calendar.py add <title> [minutes]  # Add event
"""

import json
import subprocess
import sys
import os
import re
from shutil import which
from datetime import datetime, timedelta, timezone
import uuid

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ====== Config ======
CONFIG_DIR = os.path.expanduser("~/.tinyPeople/conf/icloud-calendar")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Global variables
config = {}
USER_ID = None
CALENDARS = {}
CALDAV_URL = ""
CONFIG_PATH = ""


def _resolve_curl_bin():
    """Resolve curl binary for environments (like Passenger) with restricted PATH."""
    for candidate in ("/usr/bin/curl", "/bin/curl", which("curl")):
        if candidate and os.path.exists(candidate):
            return candidate
    return "curl"


CURL_BIN = _resolve_curl_bin()


def _debug_flag_path():
    """Return debug flag file path (checked at runtime; no restart needed)."""
    cfg_path = (config.get("bridge", {}) or {}).get("debug_flag_file", "")
    if cfg_path:
        return cfg_path
    return os.path.join(os.path.dirname(__file__), "tmp", "bridge-debug.on")


def _debug_enabled():
    """Runtime debug toggle.

    Priority:
      1) ICLOUD_BRIDGE_DEBUG env var (1/true/on)
      2) Presence of debug flag file
    """
    env = os.environ.get("ICLOUD_BRIDGE_DEBUG", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return os.path.exists(_debug_flag_path())


def _debug_log(message):
    if _debug_enabled():
        print(f"[bridge-debug][calendar] {message}", file=sys.stderr, flush=True)


def _local_now_naive():
    """Return local wall-clock time as naive datetime for stable downstream behavior."""
    return datetime.now().astimezone().replace(tzinfo=None)


def _resolve_zoneinfo(tzid):
    if not tzid or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tzid)
    except Exception:
        return None


def _configured_event_timezone():
    """Optional IANA timezone used when creating events (e.g., Europe/Berlin)."""
    tzid = (config.get("event_timezone", "") or os.environ.get("ICLOUD_EVENT_TZ", "")).strip()
    return tzid


def _parse_dtstart_from_line(line):
    """Parse DTSTART lines that may be date-only or date-time with optional seconds/Z."""
    # Examples:
    # DTSTART;TZID=Pacific/Auckland:20260301T215753
    # DTSTART:20260301T2157
    # DTSTART:20260301T215700Z
    # DTSTART;VALUE=DATE:20260301
    match = re.search(r'DTSTART(?P<params>[^:]*):(?P<value>[^\r\n]+)', line)
    if not match:
        return None

    params = match.group("params") or ""
    value = (match.group("value") or "").strip()

    # All-day events often arrive as VALUE=DATE:YYYYMMDD.
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        dt = datetime.strptime(value[:8], "%Y%m%d")
        return dt

    tz_match = re.search(r'TZID=([^;:]+)', params, flags=re.I)
    tzid = tz_match.group(1) if tz_match else ""

    # Accept HHMM or HHMMSS forms, with optional trailing Z.
    raw = value.rstrip("Z")
    match_dt = re.fullmatch(r'(\d{8})T(\d{2})(\d{2})(\d{0,2})', raw)
    if not match_dt:
        return None

    ymd = match_dt.group(1)
    hh = match_dt.group(2)
    mm = match_dt.group(3)
    dt = datetime.strptime(f"{ymd}T{hh}{mm}", "%Y%m%dT%H%M")

    local_tz = datetime.now().astimezone().tzinfo
    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None)

    event_tz = _resolve_zoneinfo(tzid)
    if event_tz:
        return dt.replace(tzinfo=event_tz).astimezone(local_tz).replace(tzinfo=None)

    # Floating time (no explicit zone): treat as local wall-clock.
    return dt

def _config_candidates():
    """Return possible config paths in priority order."""
    candidates = []

    # 1) Explicit override for runtimes where HOME differs.
    cfg_env = os.environ.get("ICLOUD_CALENDAR_CONFIG", "").strip()
    if cfg_env:
        candidates.append(cfg_env)

    # 2) Standard home-based config path.
    candidates.append(CONFIG_FILE)

    # 3) App-local fallback (useful for troubleshooting).
    candidates.append(os.path.join(os.path.dirname(__file__), "config.json"))

    # De-duplicate while preserving order.
    unique = []
    for path in candidates:
        if path and path not in unique:
            unique.append(path)
    return unique


def load_config():
    global config, USER_ID, CALENDARS, CALDAV_URL, CONFIG_PATH
    for cfg_path in _config_candidates():
        if not os.path.exists(cfg_path):
            continue
        with open(cfg_path, 'r') as f:
            config = json.load(f)
        USER_ID = config.get("user_id", "")
        CALENDARS = config.get("calendars", {})
        CALDAV_URL = config.get("caldav_url", "https://caldav.icloud.com")
        CONFIG_PATH = cfg_path
        return True
    return False

# Load config
if not load_config():
    print("Error: Config file not found. Checked candidates:")
    for candidate in _config_candidates():
        print(f"  - {candidate}")
    sys.exit(1)

apple_id = config.get("apple_id", "")
app_password = config.get("app_password", "")

def run_curl(method, url, data=None, headers=None, check_returncode=False):
    cmd = [CURL_BIN, "-s", "-X", method, url, "-u", f"{apple_id}:{app_password}"]
    if headers:
        for h in headers:
            cmd.extend(["-H", h])
    if data:
        cmd.extend(["-d", data])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        _debug_log(f"curl failed method={method} url={url} bin={CURL_BIN}")
        if check_returncode:
            return False
        return ""
    _debug_log(f"curl method={method} url={url} rc={result.returncode} bytes={len(result.stdout or '')}")
    if check_returncode:
        return result.returncode == 0
    return result.stdout

def query_calendar_events(cal_id, start_offset_hours=-1, end_offset_days=7):
    """Query calendar events"""
    url = f"{CALDAV_URL}/{USER_ID}/calendars/{cal_id}"
    
    now = datetime.now(timezone.utc)
    start = (now + timedelta(hours=start_offset_hours)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(days=end_offset_days)).strftime("%Y%m%dT%H%M%SZ")
    
    data = f'''<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"/></c:filter>
</c:calendar-query>'''
    
    result = run_curl("REPORT", url, data=data, headers=["Content-Type: application/xml; charset=utf-8", "Depth: 1"])
    return result


def _normalize_ical_payload(payload):
    """Normalize iCal text from CalDAV XML bodies into line-oriented content."""
    text = payload or ""

    # Some servers embed escaped CR/LF sequences inside XML calendar-data.
    if "\\r\\n" in text or "\\n" in text or "\\r" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # RFC5545 folding: continuation lines begin with a space/tab.
    text = re.sub(r"\n[ \t]", "", text)
    return text


def _extract_ical_field(block, field_name):
    """Extract a field value allowing optional iCal parameters."""
    pattern = rf'(?im)^{re.escape(field_name)}(?:;[^:\r\n]*)?:(?P<value>[^\r\n]*)'
    match = re.search(pattern, block)
    if not match:
        return ""
    return (match.group("value") or "").strip()


def _iter_vevent_blocks(payload):
    """Yield VEVENT payloads from a CalDAV REPORT response."""
    normalized = _normalize_ical_payload(payload)
    return re.finditer(r'BEGIN:VEVENT\n(?P<body>.*?)\nEND:VEVENT', normalized, flags=re.S | re.I)

def parse_events(result, now, filter_minutes=30):
    """Parse events"""
    events = []
    for match in _iter_vevent_blocks(result):
        block = match.group("body")

        dt_line_match = re.search(r'(?im)^DTSTART[^:\r\n]*:[^\r\n]+', block)
        if not dt_line_match:
            continue

        dt = _parse_dtstart_from_line(dt_line_match.group(0))
        summary = _extract_ical_field(block, "SUMMARY")
        if not dt or not summary:
            continue

        try:
            diff = (dt - now).total_seconds() / 60
            if 0 <= diff < filter_minutes:
                events.append({
                    'summary': summary,
                    'time': dt.strftime("%m-%d %H:%M"),
                    'minutes_until': int(diff),
                    'calendar': 'Unknown'
                })
        except Exception:
            continue
    
    return events

def list_calendars():
    """List all calendars - read from config"""
    # Read calendar mapping from config file
    return CALENDARS


def get_events_list(days=7, limit=20):
    """Return events for the next N days"""
    now = _local_now_naive()
    all_events = []

    for cal_name, cal_id in CALENDARS.items():
        try:
            result = query_calendar_events(cal_id, start_offset_hours=-24, end_offset_days=days)
            events = parse_events(result, now, filter_minutes=max(days * 1440, 1))
            _debug_log(f"list cal={cal_name} id={cal_id} parsed_events={len(events)}")
            for event in events:
                event['calendar'] = cal_name
                all_events.append(event)
        except Exception as e:
            # Keep endpoint responsive even if one calendar fails.
            _debug_log(f"list cal={cal_name} id={cal_id} failed error={type(e).__name__}: {e}")
            continue

    all_events.sort(key=lambda x: x.get('minutes_until', 99999))
    return {"events": all_events[:max(limit, 1)]}


def get_upcoming_events(minutes=30):
    """Return events in the next N minutes"""
    now = _local_now_naive()
    all_events = []

    for cal_name, cal_id in CALENDARS.items():
        try:
            result = query_calendar_events(cal_id, start_offset_hours=-1, end_offset_days=1)
            events = parse_events(result, now, filter_minutes=max(minutes, 1))
            _debug_log(f"upcoming cal={cal_name} id={cal_id} parsed_events={len(events)}")
            for event in events:
                event['calendar'] = cal_name
                all_events.append(event)
        except Exception as e:
            # Keep endpoint responsive even if one calendar fails.
            _debug_log(f"upcoming cal={cal_name} id={cal_id} failed error={type(e).__name__}: {e}")
            continue

    all_events.sort(key=lambda x: x.get('minutes_until', 99999))
    return {"upcoming": all_events}


def get_today_events():
    """Return today's events"""
    now = _local_now_naive()
    today_end = datetime(now.year, now.month, now.day, 23, 59)
    filter_minutes = max(int((today_end - now).total_seconds() / 60), 1)
    all_events = []

    for cal_name, cal_id in CALENDARS.items():
        try:
            result = query_calendar_events(cal_id, start_offset_hours=-1, end_offset_days=1)
            events = parse_events(result, now, filter_minutes=filter_minutes)
            _debug_log(f"today cal={cal_name} id={cal_id} parsed_events={len(events)}")
            for event in events:
                event['calendar'] = cal_name
                all_events.append(event)
        except Exception as e:
            # Keep endpoint responsive even if one calendar fails.
            _debug_log(f"today cal={cal_name} id={cal_id} failed error={type(e).__name__}: {e}")
            continue

    all_events.sort(key=lambda x: x.get('minutes_until', 99999))
    return {"today": all_events}

def cmd_list():
    """List all events (next 7 days)"""
    print(json.dumps(get_events_list(days=7, limit=20), indent=2, ensure_ascii=False))

def cmd_upcoming():
    """Get events in the next 30 minutes"""
    print(json.dumps(get_upcoming_events(minutes=30), indent=2, ensure_ascii=False))

def cmd_today():
    """Get today's events"""
    print(json.dumps(get_today_events(), indent=2, ensure_ascii=False))

def cmd_add(summary, description="", minutes=20):
    """Add event"""
    # Default: add to first available calendar
    calendar_name = config.get("default_calendar", "") or (list(CALENDARS.keys())[0] if CALENDARS else "")
    cal_id = CALENDARS.get(calendar_name, "")
    
    if not cal_id:
        return {"error": f"Calendar not found: {calendar_name}"}
    
    tzid = _configured_event_timezone()
    event_tz = _resolve_zoneinfo(tzid)

    # Calculate time in configured timezone (or UTC by default).
    now_for_event = datetime.now(event_tz) if event_tz else datetime.now(timezone.utc)
    start_time = now_for_event + timedelta(minutes=minutes)
    end_time = start_time + timedelta(minutes=20)
    
    # Generate unique ID
    event_uid = str(uuid.uuid4())
    
    if event_tz and tzid:
        dtstart_line = f"DTSTART;TZID={tzid}:{start_time.strftime('%Y%m%dT%H%M%S')}"
        dtend_line = f"DTEND;TZID={tzid}:{end_time.strftime('%Y%m%dT%H%M%S')}"
    else:
        dtstart_line = f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%SZ')}"
        dtend_line = f"DTEND:{end_time.strftime('%Y%m%dT%H%M%SZ')}"

    # Build iCal format
    ical = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
{dtstart_line}
{dtend_line}
SUMMARY:{summary}
DESCRIPTION:{description}
UID:{event_uid}
END:VEVENT
END:VCALENDAR"""
    
    url = f"{CALDAV_URL}/{USER_ID}/calendars/{cal_id}/{event_uid}.ics"
    
    result = run_curl("PUT", url, data=ical, headers=["Content-Type: text/calendar; charset=utf-8"])
    
    return {"success": True, "message": f"Added to {calendar_name}: {summary}", "time": start_time.strftime("%H:%M"), "uid": event_uid}

def parse_events_with_uid(result, now):
    """Parse events (including UID)"""
    events = []
    for match in _iter_vevent_blocks(result):
        block = match.group("body")

        uid = _extract_ical_field(block, "UID")
        summary = _extract_ical_field(block, "SUMMARY")
        dt_line_match = re.search(r'(?im)^DTSTART[^:\r\n]*:[^\r\n]+', block)
        dt = _parse_dtstart_from_line(dt_line_match.group(0)) if dt_line_match else None

        if not (dt and uid and summary):
            continue

        try:
            events.append({
                'uid': uid,
                'summary': summary,
                'dt': dt,
                'minutes_until': int((dt - now).total_seconds() / 60)
            })
        except Exception:
            continue
    
    return events

def cmd_delete(identifier, calendar_name=None):
    """Delete event - by title or UID"""
    calendars_to_search = {}
    if calendar_name:
        cal_id = CALENDARS.get(calendar_name, "")
        if not cal_id:
            return {"error": f"Calendar not found: {calendar_name}"}
        calendars_to_search = {calendar_name: cal_id}
    else:
        calendars_to_search = CALENDARS
    
    now = _local_now_naive()
    
    # First try to delete directly as UID (only when identifier looks like a UUID)
    uuid_pattern = re.compile(r'^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$', re.I)
    if uuid_pattern.match(identifier):
        for cal_name, cal_id in calendars_to_search.items():
            url = f"{CALDAV_URL}/{USER_ID}/calendars/{cal_id}/{identifier}.ics"
            if run_curl("DELETE", url, check_returncode=True):
                return {"success": True, "message": f"Deleted event (UID): {identifier}", "calendar": cal_name}
    
    # Search by title and delete the nearest matching event
    for cal_name, cal_id in calendars_to_search.items():
        result = query_calendar_events(cal_id, start_offset_hours=-24, end_offset_days=7)
        events = parse_events_with_uid(result, now)
        
        for event in events:
            if identifier.lower() in event.get('summary', '').lower():
                event_uid = event.get('uid', '')
                if event_uid:
                    url = f"{CALDAV_URL}/{USER_ID}/calendars/{cal_id}/{event_uid}.ics"
                    if run_curl("DELETE", url, check_returncode=True):
                        return {"success": True, "message": f"Deleted event: {event['summary']}", "calendar": cal_name}
    
    return {"error": f"Event not found: {identifier}"}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 icloud_calendar.py list          # List all events (next 7 days)")
        print("  python3 icloud_calendar.py upcoming     # Get events in the next 30 minutes")
        print("  python3 icloud_calendar.py today         # Get today's events")
        print("  python3 icloud_calendar.py calendars     # Get calendar list")
        print("  python3 icloud_calendar.py add <title> [minutes]  # Add event")
        print("  python3 icloud_calendar.py delete <title or UID> [calendar name]  # Delete event")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "list":
        cmd_list()
    elif cmd == "upcoming":
        cmd_upcoming()
    elif cmd == "today":
        cmd_today()
    elif cmd == "calendars":
        print(json.dumps(list_calendars(), indent=2, ensure_ascii=False))
    elif cmd == "add":
        summary = sys.argv[2] if len(sys.argv) > 2 else "New Event"
        minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        result = cmd_add(summary, "", minutes)
        print(json.dumps(result, ensure_ascii=False))
    elif cmd == "delete":
        identifier = sys.argv[2] if len(sys.argv) > 2 else ""
        calendar_name = sys.argv[3] if len(sys.argv) > 3 else None
        if not identifier:
            print("Error: Please provide the event title or UID to delete")
            sys.exit(1)
        result = cmd_delete(identifier, calendar_name)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
