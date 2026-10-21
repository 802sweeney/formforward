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
  python3 strava_import.py --streams  # backfill HR streams for activities missing them
  python3 strava_import.py --laps     # backfill lap splits for activities missing them
  python3 strava_import.py --polylines  # backfill summary_polyline from raw_json (no API calls)

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
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

class _ResponseWrapper:
    def __init__(self, data, status_code, headers):
        self.data = data
        self.status_code = status_code
        self.headers = headers

    def json(self):
        return json.loads(self.data.decode('utf-8'))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise urllib.error.HTTPError(None, self.status_code, self.data.decode('utf-8', errors='ignore'), self.headers, None)

class _RequestsFallback:
    @staticmethod
    def post(url, data=None, json=None, headers=None, timeout=30):
        hdr = dict(headers or {})
        req_data = None
        if data is not None:
            req_data = urllib.parse.urlencode(data).encode('utf-8')
            hdr['Content-Type'] = 'application/x-www-form-urlencoded'
        elif json is not None:
            import json as _j
            req_data = _j.dumps(json).encode('utf-8')
            hdr['Content-Type'] = 'application/json'
        
        req = urllib.request.Request(url, data=req_data, headers=hdr, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return _ResponseWrapper(res.read(), res.status, dict(res.headers))
        except urllib.error.HTTPError as e:
            return _ResponseWrapper(e.read(), e.code, dict(e.headers))

    @staticmethod
    def get(url, params=None, headers=None, timeout=30):
        hdr = dict(headers or {})
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        req = urllib.request.Request(url, headers=hdr, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return _ResponseWrapper(res.read(), res.status, dict(res.headers))
        except urllib.error.HTTPError as e:
            return _ResponseWrapper(e.read(), e.code, dict(e.headers))

try:
    import requests
except ImportError:
    requests = _RequestsFallback()

BASE     = Path(__file__).parent
ENV_FILE = BASE / 'strava_api' / '.env'
DB_FILE  = BASE / 'training.db'

STRAVA_TOKEN_URL      = 'https://www.strava.com/oauth/token'
STRAVA_ACTIVITIES_URL = 'https://www.strava.com/api/v3/athlete/activities'
STRAVA_STREAMS_URL    = 'https://www.strava.com/api/v3/activities/{id}/streams'
STRAVA_LAPS_URL       = 'https://www.strava.com/api/v3/activities/{id}/laps'
PER_PAGE = 200   # Strava max

# Garmin 5-zone HR boundaries (bpm) — anchored at LTHR=169, MaxHR=180
HR_ZONES = [
    ('Z1', 0,   133),
    ('Z2', 134, 147),
    ('Z3', 148, 154),
    ('Z4', 155, 165),
    ('Z5', 166, 999),
]


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
        CREATE TABLE IF NOT EXISTS activity_streams (
            activity_id   INTEGER PRIMARY KEY,
            fetched_at    TEXT,
            hr_data       TEXT,   -- JSON array of bpm (1 sample/sec)
            altitude_data TEXT,   -- JSON array of metres
            distance_data TEXT,   -- JSON array of metres
            time_data     TEXT,   -- JSON array of seconds from start
            z1_secs       INTEGER DEFAULT 0,
            z2_secs       INTEGER DEFAULT 0,
            z3_secs       INTEGER DEFAULT 0,
            z4_secs       INTEGER DEFAULT 0,
            z5_secs       INTEGER DEFAULT 0,
            workout_type  TEXT,   -- inferred: recovery, z2, sweet_spot, race_sim, vo2max, mixed
            quality_notes TEXT    -- flags like "went too hard", "not hard enough"
        )
    ''')

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
            summary_polyline         TEXT,     -- Google encoded polyline for route matching
            raw_json                 TEXT      -- full API response, nothing lost
        )
    ''')

    # Migration: add summary_polyline if missing (for existing DBs)
    try:
        conn.execute('ALTER TABLE strava_activities ADD COLUMN summary_polyline TEXT')
    except Exception:
        pass  # column already exists

    conn.execute('''
        CREATE TABLE IF NOT EXISTS activity_laps (
            id               INTEGER PRIMARY KEY,  -- Strava lap ID
            activity_id      INTEGER,
            lap_index        INTEGER,
            name             TEXT,
            elapsed_time     INTEGER,   -- seconds
            moving_time      INTEGER,   -- seconds
            distance         REAL,      -- meters
            elevation_gain   REAL,      -- meters
            avg_heartrate    REAL,
            max_heartrate    REAL,
            avg_speed        REAL,      -- m/s
            avg_watts        REAL,
            device_watts     INTEGER,
            start_date_local TEXT,
            start_index      INTEGER,   -- index into activity stream
            end_index        INTEGER,
            z1_secs          INTEGER DEFAULT 0,
            z2_secs          INTEGER DEFAULT 0,
            z3_secs          INTEGER DEFAULT 0,
            z4_secs          INTEGER DEFAULT 0,
            z5_secs          INTEGER DEFAULT 0,
            fetched_at       TEXT
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS strava_sync (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Migration: add anchor columns to workout_menu if missing
    for col in ('anchor_lat REAL', 'anchor_lng REAL', 'trailforks_url TEXT'):
        try:
            conn.execute(f'ALTER TABLE workout_menu ADD COLUMN {col}')
        except Exception:
            pass

    # Seed known trail anchor coordinates (Kingdom Trails / Dashney zone)
    # Dead Moose Alley was initially seeded with wrong coords (Moose Alley) — force-correct it.
    # GPS-verified 2026-07-21: July 16 "dead moose alley uphill" polyline passed within 30m of
    # (44.5835, -71.8927), confirming this is Burke Mountain Resort, not Kingdom Trails.
    conn.execute('''
        UPDATE workout_menu SET anchor_lat=44.5835, anchor_lng=-71.8927,
            trailforks_url='https://www.trailforks.com/trails/dead-moose-alley/'
        WHERE climb='dead moose alley'
    ''')
    # Also fix 'shire to camptown' — same summit as Dead Moose Alley / Camptown (0ft apart on TF)
    conn.execute('''
        UPDATE workout_menu SET anchor_lat=44.5835, anchor_lng=-71.8927
        WHERE climb='shire to camptown' AND anchor_lat IS NULL
    ''')
    # Fix 'mo and butter' — was seeded with Butter Tubs anchor; correct anchor is Moriah's Ascent
    # GPS-verified 2026-07-21: July 20 "loops" (mo butter up) passes within 6m of (44.6120,-71.9062)
    # Mo (Moriah's Ascent) is a dedicated uphill that parallels lower Moose Alley and feeds into Butter
    conn.execute('''
        UPDATE workout_menu SET anchor_lat=44.6120, anchor_lng=-71.9062,
            trailforks_url='https://www.trailforks.com/trails/moriah-s-ascent/'
        WHERE climb='mo and butter'
    ''')

    # General seed: update rows with no anchor yet
    trail_anchors = [
        # Moose Alley (Kingdom Trails) is the recovery descent for butter loops
        ('moose alley',       44.6061, -71.8946, 'https://www.trailforks.com/trails/moose-alley/'),
        ('butter',            44.6105, -71.9024, 'https://www.trailforks.com/trails/butter-tubs/'),
        ('mo and butter',     44.6120, -71.9062, 'https://www.trailforks.com/trails/moriah-s-ascent/'),
        ('pinkham',           44.6070, -71.9189, 'https://www.trailforks.com/trails/pinkham-road-215156/'),
        # Stubborn: GPS-verified 2026-07-21 — ride polylines pass within 2–11m of this anchor
        ('stubborn',          44.6006, -71.9077, 'https://www.trailforks.com/trails/stubborn/'),
    ]
    for climb, lat, lng, url in trail_anchors:
        conn.execute('''
            UPDATE workout_menu SET anchor_lat=?, anchor_lng=?, trailforks_url=?
            WHERE climb=? AND anchor_lat IS NULL
        ''', (lat, lng, url, climb))

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

    polyline = (a.get('map') or {}).get('summary_polyline') or None

    conn.execute('''
        INSERT OR REPLACE INTO strava_activities VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        polyline,
        json.dumps(a),
    ))


# ── Streams ───────────────────────────────────────────────────────────────────

def fetch_streams(access_token, activity_id):
    """Fetch HR, altitude, distance, and time streams for one activity."""
    url = STRAVA_STREAMS_URL.format(id=activity_id)
    resp = requests.get(
        url,
        headers={'Authorization': f'Bearer {access_token}'},
        params={'keys': 'heartrate,altitude,distance,time', 'key_by_type': 'true'},
        timeout=30,
    )
    if resp.status_code == 404:
        return None   # activity has no stream data (manual entry, etc.)
    resp.raise_for_status()
    return resp.json()


def zone_for_hr(bpm):
    for name, lo, hi in HR_ZONES:
        if lo <= bpm <= hi:
            return name
    return 'Z5'


def compute_zones(hr_data, time_data):
    """Return seconds spent in each Garmin zone."""
    counts = {z[0]: 0 for z in HR_ZONES}
    if not hr_data or not time_data or len(hr_data) != len(time_data):
        return counts
    for i in range(len(hr_data) - 1):
        dt = time_data[i + 1] - time_data[i]
        if dt <= 0 or hr_data[i] is None:
            continue
        counts[zone_for_hr(int(hr_data[i]))] += dt
    return counts


def classify_workout(zones, avg_hr, moving_secs):
    """
    Infer workout type and flag quality issues based on Garmin zone distribution.
    Returns (workout_type, quality_notes).
    """
    total = sum(zones.values()) or 1
    pct = {z: zones[z] / total * 100 for z in zones}

    z3_plus = pct['Z3'] + pct['Z4'] + pct['Z5']
    z4_plus  = pct['Z4'] + pct['Z5']
    hrs = moving_secs / 3600

    notes = []

    # Classify by dominant zone pattern
    if pct['Z1'] + pct['Z2'] >= 75:
        wtype = 'recovery' if hrs < 1.25 else 'z2'
    elif pct['Z3'] >= 35 and z4_plus < 20:
        wtype = 'sweet_spot'
    elif z4_plus >= 25:
        wtype = 'race_sim' if hrs >= 1.0 else 'vo2max'
    elif z3_plus >= 40:
        wtype = 'sweet_spot'
    else:
        wtype = 'mixed'

    # Quality flags
    if wtype in ('recovery', 'z2') and z3_plus > 20:
        notes.append(f'went too hard — {z3_plus:.0f}% above Z2 (target <20%)')
    if wtype == 'sweet_spot':
        if z4_plus > 20:
            notes.append(f'ceiling breaches — {z4_plus:.0f}% in Z4-Z5 (target <15%)')
        if pct['Z3'] + pct['Z4'] < 50:
            notes.append(f'low sweet spot time — only {pct["Z3"]+pct["Z4"]:.0f}% in Z3 (target >60%)')
    if wtype == 'race_sim' and z4_plus < 20:
        notes.append(f'not hard enough — only {z4_plus:.0f}% in Z4+ (target >25% for race sim)')
    if wtype == 'vo2max' and z4_plus < 25:
        notes.append(f'not hard enough — only {z4_plus:.0f}% in Z4+ (target >30% for VO2max)')

    return wtype, '; '.join(notes) if notes else None


def process_streams(conn, access_token, activity_id, avg_hr, moving_secs):
    """Fetch, compute zones, classify, and store streams for one activity."""
    raw = fetch_streams(access_token, activity_id)
    if raw is None:
        return

    hr_data   = raw.get('heartrate', {}).get('data', [])
    alt_data  = raw.get('altitude',  {}).get('data', [])
    dist_data = raw.get('distance',  {}).get('data', [])
    time_data = raw.get('time',      {}).get('data', [])

    if not hr_data:
        return   # no HR data, nothing useful to store

    zones = compute_zones(hr_data, time_data)
    wtype, quality_notes = classify_workout(zones, avg_hr or 0, moving_secs or 0)

    conn.execute('''
        INSERT OR REPLACE INTO activity_streams
        (activity_id, fetched_at, hr_data, altitude_data, distance_data, time_data,
         z1_secs, z2_secs, z3_secs, z4_secs, z5_secs, workout_type, quality_notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        activity_id,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(hr_data),
        json.dumps(alt_data),
        json.dumps(dist_data),
        json.dumps(time_data),
        zones['Z1'], zones['Z2'], zones['Z3'], zones['Z4'], zones['Z5'],
        wtype,
        quality_notes,
    ))
    conn.commit()


# ── Laps ─────────────────────────────────────────────────────────────────────

def fetch_laps(access_token, activity_id):
    """Fetch lap splits for one activity."""
    url = STRAVA_LAPS_URL.format(id=activity_id)
    resp = requests.get(
        url,
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def compute_lap_zones(hr_data, time_data, start_index, end_index):
    """Slice the activity HR stream by lap indices and compute zone seconds."""
    counts = {z[0]: 0 for z in HR_ZONES}
    if not hr_data or not time_data:
        return counts
    sl_hr   = hr_data[start_index:end_index + 1]
    sl_time = time_data[start_index:end_index + 1]
    for i in range(len(sl_hr) - 1):
        dt = sl_time[i + 1] - sl_time[i]
        if dt <= 0 or sl_hr[i] is None:
            continue
        counts[zone_for_hr(int(sl_hr[i]))] += dt
    return counts


def process_laps(conn, access_token, activity_id, name=''):
    """Fetch laps from Strava, cross-reference HR stream for zone breakdown, and store."""
    laps = fetch_laps(access_token, activity_id)
    if not laps:
        return

    # Pull stream data if available (for per-lap zone breakdown)
    stream_row = conn.execute(
        'SELECT hr_data, time_data FROM activity_streams WHERE activity_id=?',
        (activity_id,)
    ).fetchone()
    hr_data   = json.loads(stream_row['hr_data'])   if stream_row else []
    time_data = json.loads(stream_row['time_data']) if stream_row else []

    now = datetime.now(timezone.utc).isoformat()

    # Delete existing laps for this activity before re-inserting
    conn.execute('DELETE FROM activity_laps WHERE activity_id=?', (activity_id,))

    for lap in laps:
        start_idx = lap.get('start_index', 0)
        end_idx   = lap.get('end_index',   0)
        zones = compute_lap_zones(hr_data, time_data, start_idx, end_idx)

        conn.execute('''
            INSERT OR REPLACE INTO activity_laps
            (id, activity_id, lap_index, name, elapsed_time, moving_time,
             distance, elevation_gain, avg_heartrate, max_heartrate,
             avg_speed, avg_watts, device_watts, start_date_local,
             start_index, end_index,
             z1_secs, z2_secs, z3_secs, z4_secs, z5_secs, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            lap.get('id'),
            activity_id,
            lap.get('lap_index'),
            lap.get('name'),
            lap.get('elapsed_time'),
            lap.get('moving_time'),
            lap.get('distance'),
            lap.get('total_elevation_gain'),
            lap.get('average_heartrate'),
            lap.get('max_heartrate'),
            lap.get('average_speed'),
            lap.get('average_watts'),
            int(bool(lap.get('device_watts'))),
            lap.get('start_date_local'),
            start_idx,
            end_idx,
            zones['Z1'], zones['Z2'], zones['Z3'], zones['Z4'], zones['Z5'],
            now,
        ))

    conn.commit()
    return len(laps)


def backfill_polylines(conn):
    """Extract summary_polyline from stored raw_json — no API calls needed."""
    rows = conn.execute(
        'SELECT id, name, raw_json FROM strava_activities WHERE summary_polyline IS NULL AND raw_json IS NOT NULL ORDER BY start_date DESC'
    ).fetchall()
    print(f'  Backfilling polylines for {len(rows)} activities…')
    updated = 0
    for row in rows:
        try:
            raw = json.loads(row['raw_json'])
            polyline = (raw.get('map') or {}).get('summary_polyline')
            if polyline:
                conn.execute('UPDATE strava_activities SET summary_polyline=? WHERE id=?', (polyline, row['id']))
                updated += 1
        except Exception:
            pass
    conn.commit()
    print(f'  Done — {updated} polylines extracted.')


def backfill_laps(conn, access_token, limit=50):
    """Fetch laps for activities that don't have them yet."""
    rows = conn.execute('''
        SELECT a.id, a.name
        FROM strava_activities a
        LEFT JOIN activity_laps l ON l.activity_id = a.id
        WHERE l.activity_id IS NULL
          AND a.has_heartrate = 1
        ORDER BY a.start_date DESC
        LIMIT ?
    ''', (limit,)).fetchall()

    if not rows:
        print("✅  All activities already have lap data.")
        return

    print(f"📡  Fetching laps for {len(rows)} activities…\n")
    for i, row in enumerate(rows):
        act_id, name = row['id'], row['name']
        try:
            n = process_laps(conn, access_token, act_id, name)
            print(f"  {i+1:>3}. {name[:50]:<50}  {n} laps")
        except Exception as e:
            print(f"  ⚠️  {name[:50]} — laps error: {e}")
        time.sleep(0.2)

    print(f"\n✅  Done.")


def backfill_streams(conn, access_token, limit=50):
    """Fetch streams for activities that don't have them yet."""
    rows = conn.execute('''
        SELECT a.id, a.average_heartrate, a.moving_time, a.name
        FROM strava_activities a
        LEFT JOIN activity_streams s ON s.activity_id = a.id
        WHERE s.activity_id IS NULL
          AND a.has_heartrate = 1
        ORDER BY a.start_date DESC
        LIMIT ?
    ''', (limit,)).fetchall()

    if not rows:
        print("✅  All activities already have stream data.")
        return

    print(f"📡  Fetching streams for {len(rows)} activities…\n")
    for i, row in enumerate(rows):
        act_id, avg_hr, moving_secs, name = row
        try:
            process_streams(conn, access_token, act_id, avg_hr, moving_secs)
            # Look up what was stored
            stored = conn.execute(
                'SELECT workout_type, quality_notes FROM activity_streams WHERE activity_id=?',
                (act_id,)
            ).fetchone()
            wtype = stored['workout_type'] if stored else '?'
            flag  = f'  ⚠️  {stored["quality_notes"]}' if stored and stored['quality_notes'] else ''
            print(f"  {i+1:>3}. {name[:45]:<45}  [{wtype}]{flag}")
        except Exception as e:
            print(f"  ⚠️  {name[:45]} — stream error: {e}")
        time.sleep(0.2)  # stay well within rate limits

    print(f"\n✅  Done.")

    # Trigger a snapshot update so Claude can read the fresh zone data
    try:
        import sys
        sys.path.insert(0, str(BASE))
        from server import write_snapshot
        write_snapshot()
        print("📄  training_snapshot.json updated.")
    except Exception as e:
        print(f"⚠️   Snapshot update failed: {e} — restart the server to refresh it.")


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
    """Pull any activities newer than the most recent one we have (looking back 7 days for safety)."""
    newest = newest_activity_ts(conn)
    if newest is None:
        print("No existing data — run a backward sync first.")
        return

    # Look back 7 days from newest activity to ensure out-of-order or multi-activity days are never missed
    after_ts = newest - (7 * 86400)
    after_date = datetime.fromtimestamp(after_ts, tz=timezone.utc).strftime('%Y-%m-%d')
    print(f"🔄  Checking for new activities since {after_date}…")

    new_ids = []
    run_total = 0
    try:
        while True:
            activities = fetch_page(access_token, after=after_ts)
            if not activities:
                break
            for a in activities:
                # Check if this is truly new
                exists = conn.execute('SELECT id FROM strava_activities WHERE id=?', (a['id'],)).fetchone()
                upsert_activity(conn, a)
                if not exists:
                    run_total += 1
                    if a.get('has_heartrate'):
                        new_ids.append((a['id'], a.get('average_heartrate'), a.get('moving_time'), a.get('name','')))
            conn.commit()
            if len(activities) < PER_PAGE:
                break
            # Advance page
            after_ts = max(
                int(datetime.fromisoformat(
                    a['start_date'].replace('Z', '+00:00')
                ).timestamp())
                for a in activities
            )
            time.sleep(0.15)

    except RateLimitReached as e:
        print(f"⚠️   Rate limit: {e}")

    print(f"✅  {run_total} new activities added.")

    # Fetch streams + laps for new activities and report quality
    if new_ids:
        print(f"\n📡  Fetching streams + laps for {len(new_ids)} new activit{'y' if len(new_ids)==1 else 'ies'}…")
        for act_id, avg_hr, moving_secs, name in new_ids:
            try:
                process_streams(conn, access_token, act_id, avg_hr, moving_secs)
                # Fetch laps after streams so zone breakdown is available
                n_laps = process_laps(conn, access_token, act_id, name)
                stored = conn.execute(
                    'SELECT workout_type, quality_notes FROM activity_streams WHERE activity_id=?',
                    (act_id,)
                ).fetchone()
                wtype  = stored['workout_type'] if stored else '?'
                flag   = f'\n    ⚠️  {stored["quality_notes"]}' if stored and stored['quality_notes'] else ''
                laps_s = f'  {n_laps} laps' if n_laps else ''
                print(f"  → {name[:50]}  [{wtype}]{laps_s}{flag}")
            except Exception as e:
                print(f"  ⚠️  {name[:50]} — error: {e}")
            time.sleep(0.3)

    total = conn.execute('SELECT COUNT(*) FROM strava_activities').fetchone()[0]
    print(f"\n📊  Activities in database: {total}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Import Strava history into training.db')
    parser.add_argument('--reset',   action='store_true', help='Reset backward sync cursor and start over from yesterday')
    parser.add_argument('--forward', action='store_true', help='Only pull new activities since most recent stored')
    parser.add_argument('--streams', action='store_true', help='Backfill HR streams for activities missing them (up to 50 at a time)')
    parser.add_argument('--laps',    action='store_true', help='Backfill lap splits for activities missing them (up to 50 at a time)')
    parser.add_argument('--polylines', action='store_true', help='Backfill summary_polyline from stored raw_json (no API calls needed)')
    args = parser.parse_args()

    print('🚴  Strava → SQLite\n')

    import sys
    try:
        env = load_env()
    except FileNotFoundError as e:
        print(f"❌  {e}")
        sys.exit(1)

    client_id     = env.get('STRAVA_CLIENT_ID')
    client_secret = env.get('STRAVA_CLIENT_SECRET')
    ref_token     = env.get('STRAVA_REFRESH_TOKEN')

    if not all([client_id, client_secret, ref_token]):
        print("❌  STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN"
              " missing from strava_api/.env")
        sys.exit(1)

    conn = get_db()

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
        sys.exit(1)

    if args.polylines:
        init_tables(conn)
        backfill_polylines(conn)
        conn.close()
        return
    elif args.streams:
        init_tables(conn)
        backfill_streams(conn, access_token)
    elif args.laps:
        init_tables(conn)
        backfill_laps(conn, access_token)
    elif args.forward:
        init_tables(conn)
        forward_sync(conn, access_token)
    else:
        init_tables(conn)
        backward_sync(conn, access_token)

    conn.close()


if __name__ == '__main__':
    main()
