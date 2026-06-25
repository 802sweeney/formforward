#!/usr/bin/env python3
"""
strava_import.py — Pull Strava activity history into training.db

HOW IT WORKS
  Starts from yesterday and pages backwards through your Strava history,
  storing every activity in the strava_activities table. Progress is saved
  after every page, so you can stop at any time (Ctrl+C or rate limit) and
  pick up exactly where you left off next run.

USAGE
  python3 strava_import.py            # resume from last saved position
  python3 strava_import.py --reset    # start over from yesterday
  python3 strava_import.py --forward  # only pull new activities since last sync

RATE LIMITS (Strava)
  100 requests / 15 minutes
  1000 requests / day
  Each page = 1 request = up to 200 activities. Script stops cleanly when
  limits are close and prints instructions to resume.

CREDENTIALS
  Reads from strava_api/.env (STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
  STRAVA_REFRESH_TOKEN). Same file your existing sync script uses.
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌  'requests' not installed. Run: pip install requests")
    raise SystemExit(1)

BASE     = Path(__file__).parent
ENV_FILE = BASE / 'strava_api' / '.env'
DB_FILE  = BASE / 'training.db'

STRAVA_TOKEN_URL      = 'https://www.strava.com/oauth/token'
STRAVA_ACTIVITIES_URL = 'https://www.strava.com/api/v3/athlete/activities'
PER_PAGE = 200   # Strava max


# ── Credentials ───────────────────────────────────────────────────────────────

def load_env():
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"No .env found at {ENV_FILE}")
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


def refresh_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(STRAVA_TOKEN_URL, data={
        'client_id':     client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type':    'refresh_token',
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()['access_token']


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn):
    """Create strava tables if they don't exist yet."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS strava_activities (
            id                       INTEGER PRIMARY KEY,
            name                     TEXT,
            sport_type               TEXT,
            type                     TEXT,
            start_date               TEXT,   -- UTC ISO8601
            start_date_local         TEXT,   -- local ISO8601
            start_date_local_date    TEXT,   -- YYYY-MM-DD for easy joining to workouts table
            timezone                 TEXT,
            utc_offset               REAL,
            moving_time              INTEGER,  -- seconds
            elapsed_time             INTEGER,  -- seconds
            distance                 REAL,     -- metres
            total_elevation_gain     REAL,     -- metres
            elev_high                REAL,
            elev_low                 REAL,
            average_speed            REAL,     -- m/s
            max_speed                REAL,
            average_watts            REAL,
            max_watts                REAL,
            weighted_average_watts   REAL,     -- normalised power
            kilojoules               REAL,
            device_watts             INTEGER,  -- 0/1 bool
            has_heartrate            INTEGER,
            average_heartrate        REAL,
            max_heartrate            REAL,
            average_cadence          REAL,
            calories                 REAL,
            suffer_score             REAL,     -- Strava's HR-based load score
            workout_type             INTEGER,
            trainer                  INTEGER,
            commute                  INTEGER,
            manual                   INTEGER,
            private                  INTEGER,
            visibility               TEXT,
            gear_id                  TEXT,
            kudos_count              INTEGER,
            achievement_count        INTEGER,
            pr_count                 INTEGER,
            description              TEXT,
            raw_json                 TEXT      -- full API response, nothing lost
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS strava_sync (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()


# ── Sync state ────────────────────────────────────────────────────────────────

def get_state(conn, key, default=None):
    row = conn.execute('SELECT value FROM strava_sync WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default

def set_state(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO strava_sync (key,value) VALUES (?,?)', (key, str(value)))
    conn.commit()

def yesterday_midnight_ts():
    """Unix timestamp for start of yesterday UTC."""
    d = datetime.now(timezone.utc).date() - timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())

def newest_activity_ts(conn):
    """Timestamp of the most recent activity we have, for forward syncs."""
    row = conn.execute(
        "SELECT start_date FROM strava_activities ORDER BY start_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return int(datetime.fromisoformat(row['start_date'].replace('Z', '+00:00')).timestamp())


# ── Strava API ────────────────────────────────────────────────────────────────

class RateLimitReached(Exception):
    pass

def fetch_page(access_token, *, before=None, after=None):
    """
    Fetch one page of activities.
    before: only activities starting before this unix timestamp
    after:  only activities starting after this unix timestamp
    """
    params = {'per_page': PER_PAGE, 'page': 1}
    if before is not None:
        params['before'] = before
    if after is not None:
        params['after'] = after

    resp = requests.get(
        STRAVA_ACTIVITIES_URL,
        headers={'Authorization': f'Bearer {access_token}'},
        params=params,
        timeout=30,
    )

    # Parse rate-limit headers before raising
    usage_hdr = resp.headers.get('X-RateLimit-Usage', '')
    limit_hdr = resp.headers.get('X-RateLimit-Limit', '')
    if usage_hdr and limit_hdr:
        short_used  = int(usage_hdr.split(',')[0])
        short_limit = int(limit_hdr.split(',')[0])
        daily_used  = int(usage_hdr.split(',')[1])
        daily_limit = int(limit_hdr.split(',')[1])
        if short_used >= short_limit - 3 or daily_used >= daily_limit - 5:
            raise RateLimitReached(
                f"15-min: {short_used}/{short_limit}  daily: {daily_used}/{daily_limit}"
            )

    if resp.status_code == 429:
        raise RateLimitReached("HTTP 429 — rate limit hit")

    resp.raise_for_status()
    return resp.json()


# ── Activity persistence ──────────────────────────────────────────────────────

def upsert_activity(conn, a):
    local = a.get('start_date_local', '')
    local_date = local[:10] if local else None   # YYYY-MM-DD

    conn.execute('''
        INSERT OR REPLACE INTO strava_activities VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        a.get('id'),
        a.get('name'),
        a.get('sport_type'),
        a.get('type'),
        a.get('start_date'),
        a.get('start_date_local'),
        local_date,
        a.get('timezone'),
        a.get('utc_offset'),
        a.get('moving_time'),
        a.get('elapsed_time'),
        a.get('distance'),
        a.get('total_elevation_gain'),
        a.get('elev_high'),
        a.get('elev_low'),
        a.get('average_speed'),
        a.get('max_speed'),
        a.get('average_watts'),
        a.get('max_watts'),
        a.get('weighted_average_watts'),
        a.get('kilojoules'),
        int(bool(a.get('device_watts'))),
        int(bool(a.get('has_heartrate'))),
        a.get('average_heartrate'),
        a.get('max_heartrate'),
        a.get('average_cadence'),
        a.get('calories'),
        a.get('suffer_score'),
        a.get('workout_type'),
        int(bool(a.get('trainer'))),
        int(bool(a.get('commute'))),
        int(bool(a.get('manual'))),
        int(bool(a.get('private'))),
        a.get('visibility'),
        a.get('gear_id'),
        a.get('kudos_count', 0),
        a.get('achievement_count', 0),
        a.get('pr_count', 0),
        a.get('description'),
        json.dumps(a),
    ))


# ── Main sync logic ───────────────────────────────────────────────────────────

def backward_sync(conn, access_token):
    """
    Page backwards from yesterday until we run out of history or hit a rate limit.
    Saves a cursor (oldest timestamp fetched) after every page.
    """
    cursor_str = get_state(conn, 'backward_cursor')
    cursor     = int(cursor_str) if cursor_str else yesterday_midnight_ts()
    complete   = get_state(conn, 'backward_complete') == '1'

    if complete:
        print("✅  Historical sync already complete. Use --reset to start over.")
        return

    cursor_date = datetime.fromtimestamp(cursor, tz=timezone.utc).strftime('%Y-%m-%d')
    print(f"📅  Starting from {cursor_date} and going backwards…")
    print(f"    (Ctrl+C saves progress and exits cleanly)\n")

    run_total = 0

    try:
        while True:
            activities = fetch_page(access_token, before=cursor)

            if not activities:
                set_state(conn, 'backward_complete', '1')
                print(f"\n✅  Reached the beginning of your Strava history!")
                break

            for a in activities:
                upsert_activity(conn, a)
            conn.commit()

            oldest_ts = min(
                int(datetime.fromisoformat(
                    a['start_date'].replace('Z', '+00:00')
                ).timestamp())
                for a in activities
            )
            # Move cursor back 1 second so we don't re-fetch the boundary activity
            cursor = oldest_ts - 1
            set_state(conn, 'backward_cursor', cursor)

            oldest_date = datetime.fromtimestamp(oldest_ts, tz=timezone.utc).strftime('%Y-%m-%d')
            run_total += len(activities)
            print(f"  ✓  {len(activities):>3} activities  →  oldest: {oldest_date}"
                  f"  (run total: {run_total})")

            if len(activities) < PER_PAGE:
                set_state(conn, 'backward_complete', '1')
                print(f"\n✅  Reached the beginning of your Strava history!")
                break

            time.sleep(0.15)   # be polite; well within rate limits

    except RateLimitReached as e:
        print(f"\n⚠️   Rate limit reached: {e}")
        print("    Progress saved — just run again to continue.")

    except KeyboardInterrupt:
        print("\n⏸   Interrupted — progress saved.")

    finally:
        total = conn.execute('SELECT COUNT(*) FROM strava_activities').fetchone()[0]
        print(f"\n📊  Activities in database: {total}  (fetched this run: {run_total})")


def forward_sync(conn, access_token):
    """Pull any activities newer than the most recent one we have."""
    after_ts = newest_activity_ts(conn)
    if after_ts is None:
        print("No existing data — run a backward sync first.")
        return

    after_date = datetime.fromtimestamp(after_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    print(f"🔄  Checking for new activities after {after_date}…")

    run_total = 0
    try:
        while True:
            activities = fetch_page(access_token, after=after_ts)
            if not activities:
                break
            for a in activities:
                upsert_activity(conn, a)
            conn.commit()
            run_total += len(activities)
            after_ts = max(
                int(datetime.fromisoformat(
                    a['start_date'].replace('Z', '+00:00')
                ).timestamp())
                for a in activities
            )
            if len(activities) < PER_PAGE:
                break
            time.sleep(0.15)

    except RateLimitReached as e:
        print(f"⚠️   Rate limit: {e}")

    print(f"✅  {run_total} new activities added.")
    total = conn.execute('SELECT COUNT(*) FROM strava_activities').fetchone()[0]
    print(f"📊  Activities in database: {total}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Import Strava history into training.db')
    parser.add_argument('--reset',   action='store_true', help='Reset backward sync cursor and start over from yesterday')
    parser.add_argument('--forward', action='store_true', help='Only pull new activities since most recent stored')
    args = parser.parse_args()

    print('🚴  Strava → SQLite\n')

    try:
        env = load_env()
    except FileNotFoundError as e:
        print(f"❌  {e}")
        return

    client_id     = env.get('STRAVA_CLIENT_ID')
    client_secret = env.get('STRAVA_CLIENT_SECRET')
    ref_token     = env.get('STRAVA_REFRESH_TOKEN')

    if not all([client_id, client_secret, ref_token]):
        print("❌  STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN"
              " missing from strava_api/.env")
        return

    conn = get_db()
    init_tables(conn)

    if args.reset:
        conn.execute("DELETE FROM strava_sync")
        conn.commit()
        print("🔄  Sync state cleared — starting over.\n")

    print("🔑  Refreshing access token…")
    try:
        access_token = refresh_access_token(client_id, client_secret, ref_token)
        print("    OK\n")
    except Exception as e:
        print(f"❌  Token refresh failed: {e}")
        conn.close()
        return

    if args.forward:
        forward_sync(conn, access_token)
    else:
        backward_sync(conn, access_token)

    conn.close()


if __name__ == '__main__':
    main()
