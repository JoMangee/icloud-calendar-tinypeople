# iCloud Calendar Skill

> Access iCloud Calendar via CalDAV protocol

## Attribution

This project is adapted from [lemoncat7/icloud-calendar](https://github.com/lemoncat7/icloud-calendar) and has been extended for use in [tinyPeople](https://tinynature.ai/) (originally developed for .openclaw). The original code is MIT licensed, as is this adaptation.

## Features

- Read calendar events via CalDAV
- Get calendar reminders
- Add events
- Delete events
- Read-only HTTP bridge with static SHA-256 digest auth
- Multi-key rotation support (no restart needed)
- Runtime debug logging (toggle on/off without restart)
- Deploy metadata stamping (revision, build timestamp)
- Rate limiting
- Health/diagnostics endpoint

## Installation

```bash
cp icloud_calendar.py ~/.tinyPeople/scripts/
cp icloud_bridge.py ~/.tinyPeople/scripts/
mkdir -p ~/.tinyPeople/conf/icloud-calendar
cp config.example.json ~/.tinyPeople/conf/icloud-calendar/config.json
```

## Configuration

### 1. Edit the config file

Edit `~/.tinyPeople/conf/icloud-calendar/config.json`:

```json
{
  "apple_id": "your@icloud.email",
  "app_password": "your-app-password",
  "user_id": "your-user-id",
  "caldav_url": "https://pXX-caldav.icloud.com",
  "default_calendar": "",
  "event_timezone": "UTC",
  "calendars": {
    "Work": "calendar-id",
    "Family": "calendar-id"
  },
  "bridge": {
    "host": "127.0.0.1",
    "port": 8088,
    "public_base_url": "https://your-bridge-host.example",
    "read_auth": {
      "keys": {
        "agent-main": {
          "salt": "replace-with-long-random-salt",
          "key": "replace-with-long-random-key"
        }
      }
    },
    "rate_limit": {
      "enabled": true,
      "window_seconds": 60,
      "max_requests": 120
    }
  }
}
```

Timezone note:

- `event_timezone` is optional and should be an IANA zone (for example `Pacific/Auckland`, `America/New_York`, `Europe/Berlin`)
- if omitted, created events are written in UTC (`...Z`) to avoid DST ambiguity
- incoming `DTSTART` values are parsed for `TZID`, `Z` (UTC), or floating local times

### 2. Get an App Specific Password

1. Sign in at [appleid.apple.com](https://appleid.apple.com)
2. Go to **Security** -> **App-Specific Passwords**
3. Generate a new password

### 3. Get your user_id and CalDAV server

Run the following commands:

```bash
curl -s -X PROPFIND -u "your@icloud.email:your-app-password" -H "Depth: 0" --data "<propfind xmlns='DAV:'><prop><current-user-principal/></prop></propfind>" https://caldav.icloud.com/
curl -s -X PROPFIND -u "your@icloud.email:your-app-password" -H "Depth: 0" --data "<propfind xmlns='DAV:' xmlns:C='urn:ietf:params:xml:ns:caldav'><prop><C:calendar-home-set/></prop></propfind>" https://caldav.icloud.com/<YOUR_USER_ID>/principal/
```

Set:

- `user_id` from the URL segment
- `caldav_url` from `calendar-home-set` href host (for example `https://p40-caldav.icloud.com`)
- shard hosts vary by account/region and can look like `p0-caldav.icloud.com`, `p40-caldav.icloud.com`, etc.; this is expected

### 4. Get calendar IDs

Run:

```bash
python3 ~/.tinyPeople/scripts/icloud_calendar.py calendars
```

Copy the IDs into the `calendars` field in your config file.

## CLI Usage

```bash
# Get events in the next 30 minutes
python3 ~/.tinyPeople/scripts/icloud_calendar.py upcoming

# Get today's events
python3 ~/.tinyPeople/scripts/icloud_calendar.py today

# Get events for the next 7 days
python3 ~/.tinyPeople/scripts/icloud_calendar.py list

# Add an event (reminder in 30 minutes)
python3 ~/.tinyPeople/scripts/icloud_calendar.py add "Drink water" 30

# Delete an event by title
python3 ~/.tinyPeople/scripts/icloud_calendar.py delete "Drink water"

# Delete an event from a specific calendar
python3 ~/.tinyPeople/scripts/icloud_calendar.py delete "Drink water" "Work"

# Delete an event by UID
python3 ~/.tinyPeople/scripts/icloud_calendar.py delete "f4603e88-5dcc-11ef-9da0-f2b427513b45"
```

## Remote Bridge (Read-Only GET)

Use `icloud_bridge.py` to expose read endpoints for low-capability agents that can only call fixed URLs.

### Endpoints

- `GET /v1/calendars`
- `GET /v1/events/today`
- `GET /v1/events/upcoming?minutes=30`
- `GET /v1/events/list?days=7&limit=20`

### Auth model

Each request must include query params:

- `action` (one of `calendars`, `today`, `upcoming`, `list`)
- `key_id` (for rotation)
- `digest` (precomputed once)

Digest formula:

```text
sha256("{salt}:{key}:{key_id}:{action}:{path}")
```

Notes:

- `salt` and `key` stay server-side in config and are never sent by caller
- each digest is endpoint-bound via `action` + `path`
- multiple `key_id` entries are supported for key rotation; keep old and new keys side by side until clients are updated

### Precompute digests (once)

```bash
python3 - <<'PY'
import hashlib

salt = "replace-with-long-random-salt"
key = "replace-with-long-random-key"
key_id = "agent-main"
items = {
    "calendars": "/v1/calendars",
    "today": "/v1/events/today",
    "upcoming": "/v1/events/upcoming",
    "list": "/v1/events/list",
}

for action, path in items.items():
    payload = f"{salt}:{key}:{key_id}:{action}:{path}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    print(action, digest)
PY
```

### Run bridge

```bash
python3 ~/.tinyPeople/scripts/icloud_bridge.py
```

### Health endpoint

```bash
curl https://your-bridge-host.example/health
```

Returns:

```json
{
  "ok": true,
  "service": "icloud-calendar-bridge",
  "revision": "abc1234",
  "build": "2026-05-17T02:53:31Z",
  "credentials_set": true,
  "calendar_count": 9
}
```

When runtime debug is enabled, `/health` also includes:

```json
{
  "debug_flag_path": "/path/to/bridge-debug.on"
}
```

This endpoint does not require auth and is useful for:

- Checking bridge deployment status
- Verifying calendar count and credential wiring
- Checking active debug-flag location (only when debug is enabled)
- Retrieving revision/build metadata

### Debug logging (runtime toggle)

Enable debug logs without restarting the bridge:

**Option 1: Environment variable**

```bash
export ICLOUD_BRIDGE_DEBUG=1
python3 ~/.tinyPeople/scripts/icloud_bridge.py
```

**Option 2: Debug flag file**

Create an empty file at the path shown in `/health` (or configure it in `bridge.debug_flag_file`):

```bash
touch /tmp/bridge-debug.on
```

The bridge checks this file on every request. Remove it to disable debug logging without restart.

**Option 3: Config file**

Set `bridge.debug_flag_file` in your config.json:

```json
{
  "bridge": {
    "debug_flag_file": "/path/to/debug-flag.on"
  }
}
```

### Example trigger URL

```text
https://your-bridge-host.example/v1/events/today?action=today&key_id=agent-main&digest=<precomputed-digest>
```

### Keep local values out of git

- Keep real values only in `~/.tinyPeople/conf/icloud-calendar/config.json`
- Use placeholders in repository files (README/example config)
- Set `bridge.public_base_url` in local config, or pass `--base-url` / `ICLOUD_BRIDGE_BASE_URL` when running `generate_digests.py`

Keep this bridge read-only. Do not expose add/delete endpoints through static digest URLs.

### Key rotation (no client downtime)

The bridge supports multiple `key_id` entries, so you can rotate keys without downtime:

**Step 1: Add a new key**

On the server, run:

```bash
python3 ~/.tinyPeople/scripts/rotate_bridge_key.py --base-url https://your-bridge-host.example
```

This will:
1. Generate a new salt and key
2. Add them to the config under a new `key_id`
3. Print fresh digests for all active keys (old and new)
4. Create a backup of the config

**Step 2: Update clients**

Update all clients to use the new digest URLs (old digests remain valid).

**Step 3: Retire the old key**

Once all clients are updated, retire the old key:

```bash
python3 rotate_bridge_key.py --retire-old agent-main
```

This removes the old `key_id` from the config.

**Options:**

```bash
# Use a specific key_id name
python3 rotate_bridge_key.py --key-id agent-v2

# Retire multiple old keys
python3 rotate_bridge_key.py --retire-old agent-main --retire-old agent-v1

# Specify config path
python3 rotate_bridge_key.py --config /path/to/config.json

# Replace an existing key (dangerous)
python3 rotate_bridge_key.py --key-id agent-main --replace-existing
```

### Rate limiting

Configure rate limiting in the `bridge.rate_limit` section of your config:

```json
{
  "bridge": {
    "rate_limit": {
      "enabled": true,
      "window_seconds": 60,
      "max_requests": 120
    }
  }
}
```

- `enabled`: boolean, default `true`
- `window_seconds`: time window in seconds, default `60`
- `max_requests`: max requests per client IP per window, default `120`

Rate-limited requests return HTTP 429.

### Deploy metadata

When deployed with cPanel/.cpanel.yml, the bridge writes a `.deploy-stamp.env` file containing:

```
POD_APP_REVISION=c3d67e2
POD_APP_BUILD=2026-05-17T02:53:31Z
```

These are exposed via the `/health` endpoint for status monitoring and version tracking.

## Commands

| Command | Description |
| ------- | ----------- |
| `list` | List events for the next 7 days |
| `upcoming` | Get events in the next 30 minutes |
| `today` | Get today's events |
| `calendars` | Get calendar list |
| `add <title> [minutes]` | Add event (default: 20 minutes from now) |
| `delete <title or UID> [calendar name]` | Delete event |

## License

MIT
