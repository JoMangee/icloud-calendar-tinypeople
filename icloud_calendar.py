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
from datetime import datetime, timedelta
import uuid

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


def _parse_dtstart_from_line(line):
    """Parse DTSTART lines that may be date-only or date-time with optional seconds/Z."""
    # Examples:
    # DTSTART;TZID=Pacific/Auckland:20260301T215753
    # DTSTART:20260301T2157
    # DTSTART:20260301T215700Z
    # DTSTART;VALUE=DATE:20260301
    match_dt = re.search(r'DTSTART[^:]*:(\d{8})T(\d{2})(\d{2})(\d{0,2})Z?', line)
    if match_dt:
        ymd = match_dt.group(1)
        hh = match_dt.group(2)
        mm = match_dt.group(3)
        # Ignore seconds for now to keep downstream minute-level behavior stable.
        return datetime.strptime(f"{ymd}T{hh}{mm}", "%Y%m%dT%H%M")

    # All-day events often arrive as VALUE=DATE:YYYYMMDD.
    match_date = re.search(r'DTSTART[^:]*:(\d{8})$', line.strip())
    if match_date:
        return datetime.strptime(match_date.group(1), "%Y%m%d")

    return None

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
    
    now = datetime.now()
    start = (now + timedelta(hours=start_offset_hours)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(days=end_offset_days)).strftime("%Y%m%dT%H%M%SZ")
    
    data = f'''<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"/></c:filter>
</c:calendar-query>'''
    
    result = run_curl("REPORT", url, data=data, headers=["Content-Type: application/xml; charset=utf-8", "Depth: 1"])
    return result

def parse_events(result, now, filter_minutes=30):
    """Parse events"""
    events = []
    lines = result.split('\n')
    current_event = {}
    in_vevent = False
    
    for line in lines:
        # Detect VEVENT boundaries
        if 'BEGIN:VEVENT' in line:
            in_vevent = True
            current_event = {}
            continue
        if 'END:VEVENT' in line:
            in_vevent = False
            if current_event.get('dt') and current_event.get('summary'):
                try:
                    dt = current_event['dt']
                    diff = (dt - now).total_seconds() / 60
                    current_event['time'] = dt.strftime("%m-%d %H:%M")
                    current_event['minutes_until'] = int(diff)
                    
                    if 0 <= diff < filter_minutes:
                        events.append({
                            'summary': current_event['summary'],
                            'time': current_event['time'],
                            'minutes_until': int(diff),
                            'calendar': current_event.get('calendar', 'Unknown')
                        })
                except:
                    pass
            current_event = {}
            continue
        
        if not in_vevent:
            continue
            
        if 'DTSTART' in line:
            dt = _parse_dtstart_from_line(line)
            if dt:
                current_event['dt'] = dt
        if 'SUMMARY' in line:
            match = re.search(r'SUMMARY:([^\r\n]+)', line)
            if match:
                current_event['summary'] = match.group(1).strip()
    
    return events

def list_calendars():
    """List all calendars - read from config"""
    # Read calendar mapping from config file
    return CALENDARS


def get_events_list(days=7, limit=20):
    """Return events for the next N days"""
    now = datetime.now()
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
    now = datetime.now()
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
    now = datetime.now()
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
    
    # Calculate time
    start_time = datetime.now() + timedelta(minutes=minutes)
    end_time = start_time + timedelta(minutes=20)
    
    # Generate unique ID
    event_uid = str(uuid.uuid4())
    
    # Build iCal format
    ical = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART;TZID=Pacific/Auckland:{start_time.strftime("%Y%m%dT%H%M%S")}
DTEND;TZID=Pacific/Auckland:{end_time.strftime("%Y%m%dT%H%M%S")}
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
    lines = result.split('\n')
    current_event = {}
    in_vevent = False
    
    for line in lines:
        # Detect VEVENT boundaries
        if 'BEGIN:VEVENT' in line:
            in_vevent = True
            current_event = {}
            continue
        if 'END:VEVENT' in line:
            in_vevent = False
            # When VEVENT ends, check if we have a complete event
            if current_event.get('dt') and current_event.get('uid') and current_event.get('summary'):
                try:
                    dt = current_event['dt']
                    current_event['minutes_until'] = int((dt - now).total_seconds() / 60)
                    events.append(current_event.copy())
                except:
                    pass
            current_event = {}
            continue
        
        if not in_vevent:
            continue
            
        if 'UID:' in line:
            match = re.search(r'UID:([^\r\n]+)', line)
            if match:
                current_event['uid'] = match.group(1).strip()
        if 'DTSTART' in line:
            dt = _parse_dtstart_from_line(line)
            if dt:
                current_event['dt'] = dt
        if 'SUMMARY' in line:
            match = re.search(r'SUMMARY:([^\r\n]+)', line)
            if match:
                current_event['summary'] = match.group(1).strip()
    
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
    
    now = datetime.now()
    
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
