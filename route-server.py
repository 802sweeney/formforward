#!/usr/bin/env python3
"""
Route Planner server — finds Strava segments that match your target intervals.
Run:  python3 route-server.py
Then: open http://localhost:5680
"""
import json
import math
import os
import sqlite3
import time
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT     = 5680
BASE     = Path(__file__).parent
ENV_FILE = BASE / 'strava_api' / '.env'
DB_FILE  = BASE / 'training.db'
HTML     = BASE / 'route-planner.html'

STRAVA_TOKEN_URL    = 'https://www.strava.com/oauth/token'
STRAVA_EXPLORE_URL  = 'https://www.strava.com/api/v3/segments/explore'
STRAVA_SEGMENT_URL  = 'https://www.strava.com/api/v3/segments/'
STRAVA_ATHLETE_URL  = 'https://www.strava.com/api/v3/athlete'

KM_TO_MI = 0.621371

# ── Credentials ───────────────────────────────────────────────────────────────

def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env

_token_cache = {'token': None, 'expires': 0}

def get_token():
    if _token_cache['token'] and time.time() < _token_cache['expires'] - 60:
        return _token_cache['token']
    env = load_env()
    data = urllib.parse.urlencode({
        'client_id':     env['STRAVA_CLIENT_ID'],
        'client_secret': env['STRAVA_CLIENT_SECRET'],
        'refresh_token': env['STRAVA_REFRESH_TOKEN'],
        'grant_type':    'refresh_token',
    }).encode()
    req  = urllib.request.Request(STRAVA_TOKEN_URL, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    _token_cache['token']   = resp['access_token']
    _token_cache['expires'] = resp['expires_at']
    return _token_cache['token']

def strava_get(url, params=None):
    token = get_token()
    if params:
        url = url + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── Database ──────────────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_home_location():
    """Return (lat, lng) from most recent outdoor ride start point."""
    try:
        with db_conn() as c:
            row = c.execute('''
                SELECT raw_json FROM strava_activities
                WHERE sport_type IN ('GravelRide','MountainBikeRide','Ride')
                  AND raw_json IS NOT NULL
                ORDER BY start_date_local DESC LIMIT 1
            ''').fetchone()
            if row:
                data = json.loads(row['raw_json'])
                ll = data.get('start_latlng')
                if ll and len(ll) == 2:
                    return {'lat': ll[0], 'lng': ll[1]}
    except Exception:
        pass
    return {'lat': 44.6054, 'lng': -71.8838}  # fallback

def get_today_workout():
    """Return today's planned workout from training DB, if any."""
    from datetime import date
    today = date.today().isoformat()
    try:
        with db_conn() as c:
            row = c.execute(
                'SELECT * FROM workouts WHERE date=?', (today,)
            ).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return None


# ── Power-speed model ─────────────────────────────────────────────────────────

def speed_at_power(watts, grade_pct, mass_kg=80.0, cda=0.36, crr=0.004, rho=1.225):
    """
    Estimate steady-state speed (m/s) at given power on given grade.
    Solves: P = 0.5*rho*CdA*v³ + (Crr*m*g*cos θ + m*g*sin θ)*v
    Using Newton's method on the cubic.
    """
    g     = 9.81
    theta = math.atan(grade_pct / 100.0)
    a     = 0.5 * rho * cda
    b     = crr * mass_kg * g * math.cos(theta) + mass_kg * g * math.sin(theta)

    # Guard: if grade is very steep and watts too low, cap at walking pace
    if b > 0 and watts / b < 0.1:
        return 0.5

    v = max(2.0, watts / (b + 1))  # initial guess
    for _ in range(60):
        f  = a * v**3 + b * v - watts
        df = 3 * a * v**2 + b
        if abs(df) < 1e-9:
            break
        v -= f / df
        v  = max(0.3, v)
        if abs(f) < 0.001:
            break
    return max(0.3, v)

def estimate_duration_min(segment, watts):
    """Estimate how many minutes a segment takes at given watts."""
    dist_m = segment.get('distance', 0)
    grade  = segment.get('avg_grade', 0)
    if dist_m <= 0:
        return None
    v = speed_at_power(watts, grade)
    return dist_m / v / 60.0

def score_segment(segment, target_min, target_watts, num_intervals):
    """
    Score 0–1 how well a segment matches the target interval.
    Also compute recommended reps (laps) to fill the interval count.
    """
    est = estimate_duration_min(segment, target_watts)
    if est is None or est <= 0:
        return None

    # How many laps of this segment to match one interval duration?
    laps_per_interval = max(1, round(target_min / est))
    effective_min     = est * laps_per_interval

    # Duration score: penalise for mismatch between effective and target
    ratio = effective_min / target_min if target_min > 0 else 1.0
    # Best when ratio=1; score degrades as it drifts
    duration_score = math.exp(-2.0 * abs(ratio - 1.0))

    # Total laps needed for all intervals
    total_laps = laps_per_interval * num_intervals

    return {
        'estimated_min':     round(est, 1),
        'laps_per_interval': laps_per_interval,
        'total_laps':        total_laps,
        'effective_min':     round(effective_min, 1),
        'score':             round(duration_score, 3),
    }


# ── Polyline decode ───────────────────────────────────────────────────────────

def decode_polyline(encoded):
    """Decode a Google/Strava encoded polyline string into [[lat, lng], …]."""
    points, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for is_lng in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            value = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += value
                points.append([lat / 1e5, lng / 1e5])
            else:
                lat += value
    return points

def fetch_segment_polyline(seg_id):
    """Fetch full segment detail and return decoded polyline, or None."""
    try:
        detail = strava_get(STRAVA_SEGMENT_URL + str(seg_id))
        encoded = detail.get('map', {}).get('polyline', '')
        if encoded:
            return decode_polyline(encoded)
    except Exception as ex:
        print(f'  polyline fetch {seg_id}: {ex}')
    return None

# ── Segment search ────────────────────────────────────────────────────────────

def bounding_box(lat, lng, radius_km):
    """Return (south, west, north, east) for a circle of radius_km."""
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lng - dlng, lat + dlat, lng + dlng

def explore_segments(lat, lng, radius_km):
    """
    Call Strava explore API across a 2x2 grid to get up to 40 segments.
    Returns deduplicated list.
    """
    seen = {}
    # Split into quadrants for better coverage
    half = radius_km / 2.0
    offsets = [(-half/111, -half/(111*math.cos(math.radians(lat)))),
               (-half/111,  half/(111*math.cos(math.radians(lat)))),
               ( half/111, -half/(111*math.cos(math.radians(lat)))),
               ( half/111,  half/(111*math.cos(math.radians(lat))))]

    for dlat, dlng in offsets:
        clat, clng = lat + dlat, lng + dlng
        s, w, n, e = bounding_box(clat, clng, radius_km * 0.7)
        try:
            result = strava_get(STRAVA_EXPLORE_URL, {
                'bounds':        f'{s},{w},{n},{e}',
                'activity_type': 'riding',
            })
            for seg in result.get('segments', []):
                if seg['id'] not in seen:
                    seen[seg['id']] = seg
        except Exception as ex:
            print(f'Explore error: {ex}')
    return list(seen.values())

def find_matching_segments(lat, lng, radius_km, target_min, target_watts, num_intervals):
    """Find and rank segments matching the target interval."""
    segments = explore_segments(lat, lng, radius_km)
    results  = []
    for seg in segments:
        match = score_segment(seg, target_min, target_watts, num_intervals)
        if match is None:
            continue
        dist_m = seg.get('distance', 0)
        results.append({
            'id':            seg['id'],
            'name':          seg['name'],
            'distance_m':    round(dist_m),
            'distance_mi':   round(dist_m / 1000 * KM_TO_MI, 2),
            'avg_grade':     seg.get('avg_grade', 0),
            'elev_diff':     seg.get('elev_difference', 0),
            'climb_cat':     seg.get('climb_category', 0),
            'start_latlng':  seg.get('start_latlng', []),
            'end_latlng':    seg.get('end_latlng', []),
            'polyline':      None,  # filled in below for top results
            **match,
        })

    results.sort(key=lambda x: -x['score'])

    # Fetch actual polylines for top 15 results
    print(f'  Fetching polylines for top {min(15, len(results))} segments…')
    for r in results[:15]:
        r['polyline'] = fetch_segment_polyline(r['id'])

    return results


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, path):
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type',   'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        p = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)

        if p in ('/', '/route-planner.html'):
            self.send_html(HTML)

        elif p == '/api/home':
            self.send_json(get_home_location())

        elif p == '/api/today-workout':
            w = get_today_workout()
            self.send_json(w or {})

        elif p == '/api/segments':
            try:
                lat          = float(q['lat'][0])
                lng          = float(q['lng'][0])
                radius_km    = float(q.get('radius_km', ['15'])[0])
                target_min   = float(q['target_min'][0])
                target_watts = float(q['target_watts'][0])
                num_intervals = int(q.get('num_intervals', ['1'])[0])
                segments = find_matching_segments(
                    lat, lng, radius_km, target_min, target_watts, num_intervals)
                self.send_json({'ok': True, 'segments': segments, 'count': len(segments)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)
        else:
            self.send_json({'error': 'not found'}, 404)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    home = get_home_location()
    print(f'\n🗺️  Route Planner')
    print(f'   Server  → http://localhost:{PORT}')
    print(f'   Home    → {home["lat"]:.4f}, {home["lng"]:.4f}')
    print(f'   Stop    → Ctrl+C\n')
    httpd = HTTPServer(('127.0.0.1', PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('Server stopped.')
