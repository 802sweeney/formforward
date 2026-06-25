#!/usr/bin/env python3
"""
Training Plan local server.
Run:  python3 server.py
Then: open http://localhost:5678
Data: training.db (SQLite, same folder as this script)
"""
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from datetime import date as dt_date, datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = 5678
BASE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(BASE, 'training.db')
HTML = os.path.join(BASE, 'training-plan.html')


# ── Database ──────────────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_conn() as c:
        # planned workouts — kept as 'workouts' table for compatibility
        c.execute('''CREATE TABLE IF NOT EXISTS workouts (
            date             TEXT PRIMARY KEY,
            name             TEXT    DEFAULT "",
            tss              REAL    DEFAULT 0,
            duration_minutes INTEGER DEFAULT 0,
            notes            TEXT    DEFAULT ""
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )''')
        # completed_workouts was a redundant derived table; strava_activities is
        # the single source of truth for completed workouts — drop the old table.
        c.execute('DROP TABLE IF EXISTS completed_workouts')
        # Manual TSS overrides — replaces computed TSS for a given date
        c.execute('''CREATE TABLE IF NOT EXISTS tss_overrides (
            date TEXT PRIMARY KEY,
            tss  REAL NOT NULL
        )''')
        c.commit()


# ── TSS calculation (mirrors strava_import.py logic) ─────────────────────────

BIKE_SPORTS = {
    'Ride', 'VirtualRide', 'GravelRide', 'MountainBikeRide',
    'EBikeRide', 'EMountainBikeRide', 'Velomobile',
}

RUN_SPORTS = {
    'Run', 'TrailRun', 'VirtualRun',
}

def calc_activity_tss(row, ftp, lthr=155):
    """Estimate TSS for one strava_activities row."""
    sport     = row['sport_type'] or ''
    move_secs = row['moving_time'] or 0
    if move_secs <= 0:
        return 0.0

    if sport in BIKE_SPORTS:
        watts = 0.0
        if row['device_watts'] and row['weighted_average_watts'] and row['weighted_average_watts'] > 0:
            watts = float(row['weighted_average_watts'])       # NP from power meter (best)
        elif row['device_watts'] and row['average_watts'] and row['average_watts'] > 0:
            watts = float(row['average_watts'])                # avg watts from power meter
        # Note: kJ-based estimation is intentionally skipped for non-power-meter rides —
        # Strava's kJ for rides without a power meter (device_watts=0) is a rough mechanical
        # energy estimate that severely underestimates actual effort. Use suffer_score instead.

        if watts > 0 and ftp > 0:
            return (move_secs * watts * watts) / (ftp * ftp * 3600) * 100
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    elif sport in RUN_SPORTS:
        # hrTSS: use average HR relative to LTHR when available
        avg_hr = row['average_heartrate'] if row['average_heartrate'] else None
        if avg_hr and avg_hr > 0 and lthr > 0:
            hr_ratio = float(avg_hr) / float(lthr)
            return (move_secs / 3600.0) * (hr_ratio ** 2) * 100
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    else:
        # Other activities: suffer_score as proxy
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    return 0.0


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all():
    with db_conn() as c:
        planned = {r['date']: {
            'name': r['name'], 'tss': r['tss'],
            'duration_minutes': r['duration_minutes'], 'notes': r['notes']
        } for r in c.execute('SELECT * FROM workouts ORDER BY date')}

        raw = {r['key']: r['value'] for r in c.execute('SELECT key, value FROM settings')}
        settings = {
            'ftp': 278, 'lthr': 155, 'ctlDecay': 42, 'atlDecay': 7,
            'startCTL': 0, 'startATL': 0, 'startDate': ''
        }
        for k, v in raw.items():
            try:    settings[k] = json.loads(v)
            except: settings[k] = v

        ftp   = float(settings.get('ftp', 278))
        lthr  = float(settings.get('lthr', 155))
        today = dt_date.today().isoformat()

        # Load manual TSS overrides
        overrides = {}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tss_overrides' in tables:
            overrides = {r['date']: r['tss'] for r in c.execute('SELECT date, tss FROM tss_overrides')}

        # Aggregate strava_activities → per-day completed workout summary
        completed = {}
        if 'strava_activities' in tables:
            rows = c.execute('''
                SELECT id, start_date_local_date, sport_type, name, moving_time,
                       weighted_average_watts, average_watts, kilojoules,
                       device_watts, suffer_score, average_heartrate
                FROM strava_activities
                WHERE start_date_local_date <= ?
                ORDER BY start_date_local_date, start_date_local
            ''', (today,)).fetchall()

            by_date = defaultdict(list)
            for row in rows:
                by_date[row['start_date_local_date']].append(row)

            for date, acts in by_date.items():
                total_tss     = 0.0
                total_minutes = 0
                names         = []
                sports        = []
                best_id       = None
                best_tss      = -1.0

                for act in acts:
                    act_tss        = calc_activity_tss(act, ftp, lthr)
                    total_tss     += act_tss
                    total_minutes += (act['moving_time'] or 0) // 60
                    names.append(act['name'] or 'Activity')
                    st = act['sport_type'] or 'Activity'
                    if st not in sports:
                        sports.append(st)
                    if act_tss > best_tss:
                        best_tss = act_tss
                        best_id  = act['id']

                display_name = (names[0] if len(acts) == 1
                                else f"{len(acts)} activities ({', '.join(sports)})")
                completed[date] = {
                    'tss':              overrides[date] if date in overrides else round(total_tss, 1),
                    'tss_override':     overrides.get(date),
                    'tss_computed':     round(total_tss, 1),
                    'duration_minutes': total_minutes,
                    'name':             display_name,
                    'sport_type':       sports[0] if sports else '',
                    'strava_id':        best_id,
                }

    return {'settings': settings, 'planned': planned, 'completed': completed}


def upsert_workout(date, w):
    with db_conn() as c:
        c.execute(
            'INSERT OR REPLACE INTO workouts (date,name,tss,duration_minutes,notes) VALUES (?,?,?,?,?)',
            (date, w.get('name',''), w.get('tss', 0), w.get('duration_minutes', 0), w.get('notes',''))
        )
        c.commit()

def delete_workout(date):
    with db_conn() as c:
        c.execute('DELETE FROM workouts WHERE date=?', (date,))
        c.commit()

def save_settings(settings):
    with db_conn() as c:
        for k, v in settings.items():
            c.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (k, json.dumps(v)))
        c.commit()

def replace_all(data):
    """Replace planned workouts and settings. Never touches strava_activities."""
    with db_conn() as c:
        c.execute('DELETE FROM workouts')
        c.execute('DELETE FROM settings')
        for date, w in data.get('planned', {}).items():
            c.execute(
                'INSERT INTO workouts (date,name,tss,duration_minutes,notes) VALUES (?,?,?,?,?)',
                (date, w.get('name',''), w.get('tss',0), w.get('duration_minutes',0), w.get('notes',''))
            )
        for k, v in data.get('settings', {}).items():
            c.execute('INSERT INTO settings (key,value) VALUES (?,?)', (k, json.dumps(v)))
        c.commit()


# ── Strava sync ───────────────────────────────────────────────────────────────

def strava_sync():
    """Pull new activities from Strava into strava_activities via strava_import.py --forward."""
    script = os.path.join(BASE, 'strava_import.py')
    if not os.path.exists(script):
        return {'ok': False, 'error': 'strava_import.py not found next to server.py'}
    try:
        result = subprocess.run(
            ['python3', script, '--forward'],
            capture_output=True, text=True, timeout=120, cwd=BASE
        )
        output = result.stdout.strip()
        # Extract activity count from output line like "✅  42 new activities added."
        new_count = 0
        for line in output.splitlines():
            if 'new activities added' in line:
                parts = line.split()
                for p in parts:
                    if p.isdigit():
                        new_count = int(p)
                        break
        return {
            'ok':            result.returncode == 0,
            'new_activities': new_count,
            'output':        output,
            'error':         result.stderr.strip() if result.returncode != 0 else '',
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'Strava sync timed out (>120s)'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
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
        if p in ('/', '/training-plan.html'):
            self.send_html(HTML)
        elif p == '/api/data':
            self.send_json(load_all())
        else:
            self.send_json({'error': 'not found'}, 404)

    def do_POST(self):
        p    = urlparse(self.path).path
        body = self.read_json()
        if p == '/api/workout':
            date = body.pop('date', None)
            if not date:
                return self.send_json({'error': 'date required'}, 400)
            upsert_workout(date, body)
            self.send_json({'ok': True})
        elif p == '/api/settings':
            save_settings(body)
            self.send_json({'ok': True})
        elif p == '/api/import':
            replace_all(body)
            self.send_json({'ok': True})
        elif p == '/api/sync-strava':
            self.send_json(strava_sync())
        elif p == '/api/tss-override':
            date = body.get('date')
            tss  = body.get('tss')
            if not date:
                return self.send_json({'error': 'date required'}, 400)
            with db_conn() as c:
                if tss is None:
                    c.execute('DELETE FROM tss_overrides WHERE date=?', (date,))
                else:
                    c.execute('INSERT OR REPLACE INTO tss_overrides (date, tss) VALUES (?,?)',
                              (date, float(tss)))
                c.commit()
            self.send_json({'ok': True})
        else:
            self.send_json({'error': 'not found'}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith('/api/workout/'):
            delete_workout(p.rsplit('/', 1)[-1])
            self.send_json({'ok': True})
        else:
            self.send_json({'error': 'not found'}, 404)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    httpd = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'\n🚴  Training Plan')
    print(f'    Server  → http://localhost:{PORT}')
    print(f'    Database→ {DB}')
    print(f'    Stop    → Ctrl+C\n')

    def _run_sync(label=''):
        result = strava_sync()
        tag = f' ({label})' if label else ''
        if result.get('ok'):
            n = result.get('new_activities', 0)
            print(f'    Strava{tag} → {n} new activit{"y" if n == 1 else "ies"} synced')
        else:
            print(f'    Strava{tag} → sync failed: {result.get("error", "unknown")}')

    def _next_scheduled():
        """Return seconds until next 9:15am on a weekday."""
        now = datetime.now()
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        # Advance past Saturday (5) and Sunday (6)
        while target.weekday() >= 5:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _scheduled_sync():
        while True:
            secs = _next_scheduled()
            next_dt = datetime.now() + timedelta(seconds=secs)
            print(f'    Strava  → next scheduled sync {next_dt.strftime("%a %b %-d at %-I:%M %p")}')
            time.sleep(secs)
            _run_sync('scheduled')

    threading.Thread(target=lambda: _run_sync('startup'), daemon=True).start()
    threading.Thread(target=_scheduled_sync, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('Server stopped.')
