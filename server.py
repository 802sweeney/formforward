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
HTML_DASHBOARD = os.path.join(BASE, 'training-load-dashboard.html')


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
    coach_engine.init_coach_db()


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
            'ftp': 278, 'lthr': 165, 'ctlDecay': 42, 'atlDecay': 7,
            'startCTL': 0, 'startATL': 0, 'startDate': ''
        }
        for k, v in raw.items():
            try:    settings[k] = json.loads(v)
            except: settings[k] = v

        ftp   = float(settings.get('ftp', 278))
        lthr  = float(settings.get('lthr', 169))
        today = dt_date.today().isoformat()

        # Load manual TSS overrides
        overrides = {}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tss_overrides' in tables:
            overrides = {r['date']: r['tss'] for r in c.execute('SELECT date, tss FROM tss_overrides')}

        # Aggregate strava_activities → per-day completed workout summary
        completed = {}
        completed_list = {}
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

            completed_list = {}
            for date, acts in by_date.items():
                total_tss     = 0.0
                total_minutes = 0
                names         = []
                sports        = []
                best_id       = None
                best_tss      = -1.0
                act_rows      = []

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
                    act_rows.append({
                        'strava_id':        act['id'],
                        'name':             act['name'] or 'Activity',
                        'sport_type':       act['sport_type'] or '',
                        'tss':              round(act_tss, 1),
                        'duration_minutes': (act['moving_time'] or 0) // 60,
                    })

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
        phases = []
        if 'phases' in tables:
            phase_rows = c.execute('SELECT start_date, end_date, name FROM phases ORDER BY start_date').fetchall()
            phases = [{'start': r['start_date'], 'end': r['end_date'], 'name': r['name']} for r in phase_rows]

    return {'settings': settings, 'planned': planned, 'completed': completed, 'completedList': completed_list, 'phases': phases}


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

def write_snapshot():
    """
    Write a JSON snapshot of the last 90 days to training_snapshot.json.
    Includes per-activity HR data and zone breakdowns so Claude can critique
    workout quality and plan the next session. Small enough for Google Drive
    to sync instantly, unlike the live SQLite file.
    """
    try:
        today     = dt_date.today().isoformat()
        since     = (dt_date.today() - timedelta(days=90)).isoformat()
        data      = load_all()
        settings  = data['settings']
        ftp       = float(settings.get('ftp', 278))
        lthr      = float(settings.get('lthr', 165))

        with db_conn() as c:
            # Recent activities with full detail
            rows = c.execute('''
                SELECT a.id, a.start_date_local_date, a.name, a.sport_type,
                       a.moving_time, a.elapsed_time, a.distance,
                       a.total_elevation_gain,
                       a.device_watts, a.weighted_average_watts, a.average_watts,
                       a.suffer_score, a.average_heartrate, a.max_heartrate,
                       a.average_cadence, a.calories, a.summary_polyline,
                       s.z1_secs, s.z2_secs, s.z3_secs, s.z4_secs, s.z5_secs,
                       s.workout_type, s.quality_notes
                FROM strava_activities a
                LEFT JOIN activity_streams s ON s.activity_id = a.id
                WHERE a.start_date_local_date >= ?
                ORDER BY a.start_date_local DESC
            ''', (since,)).fetchall()

            activities = []
            for r in rows:
                act_tss = calc_activity_tss(r, ftp, lthr)
                activities.append({
                    'id':            r['id'],
                    'date':          r['start_date_local_date'],
                    'name':          r['name'],
                    'sport_type':    r['sport_type'],
                    'moving_min':    round((r['moving_time'] or 0) / 60, 1),
                    'distance_mi':   round((r['distance'] or 0) / 1609.34, 2),
                    'elevation_ft':  round((r['total_elevation_gain'] or 0) * 3.28084),
                    'tss':           round(act_tss, 1),
                    'avg_hr':        r['average_heartrate'],
                    'max_hr':        r['max_heartrate'],
                    # Garmin 5-zone breakdown (seconds), None if streams not yet fetched
                    'z1_secs':       r['z1_secs'],
                    'z2_secs':       r['z2_secs'],
                    'z3_secs':       r['z3_secs'],
                    'z4_secs':       r['z4_secs'],
                    'z5_secs':       r['z5_secs'],
                    'workout_type':  r['workout_type'],
                    'quality_notes': r['quality_notes'],
                    'polyline':      r['summary_polyline'],
                })

        with db_conn() as c:
            phase_rows = c.execute(
                'SELECT start_date, end_date, name FROM phases ORDER BY start_date'
            ).fetchall()
            phases = [{'start': r['start_date'], 'end': r['end_date'], 'name': r['name']}
                      for r in phase_rows]

            race_rows = c.execute(
                "SELECT date, name, notes FROM workouts WHERE type='Race' ORDER BY date"
            ).fetchall()
            races = [{'date': r['date'], 'name': r['name'], 'notes': r['notes']}
                     for r in race_rows]

            menu_rows = c.execute(
                'SELECT name, interval_type, interval_min, recovery_min, hr_low, hr_high, climb, descent, anchor_lat, anchor_lng, trailforks_url FROM workout_menu ORDER BY name'
            ).fetchall()
            workout_menu = [dict(r) for r in menu_rows]

        snapshot = {
            'generated_at': datetime.now().isoformat(),
            'today':        today,
            'settings':     settings,
            'phases':       phases,
            'races':        races,
            'workout_menu': workout_menu,
            'activities':   activities,   # last 90 days, newest first
        }

        path = os.path.join(BASE, 'training_snapshot.json')
        with open(path, 'w') as f:
            json.dump(snapshot, f, indent=2)

    except Exception as e:
        print(f'    Snapshot write failed: {e}')


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
        err = result.stderr.strip()
        if not err and result.returncode != 0:
            err = output or f'Process exited with code {result.returncode}'
        return {
            'ok':            result.returncode == 0,
            'new_activities': new_count,
            'output':        output,
            'error':         err if result.returncode != 0 else '',
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'Strava sync timed out (>120s)'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ── HTTP Handler ──────────────────────────────────────────────────────────────

import coach_engine

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
        # Always write today's real date so Claude can read it reliably
        with open(os.path.join(BASE, 'current_date.txt'), 'w') as f:
            f.write(dt_date.today().isoformat())
        p = urlparse(self.path).path
        if p in ('/', '/training-plan.html'):
            self.send_html(HTML)
        elif p in ('/dashboard', '/dashboard.html', '/training-load-dashboard.html'):
            self.send_html(HTML_DASHBOARD)
        elif p == '/api/data':
            self.send_json(load_all())
        elif p == '/api/coach/context':
            ctx = coach_engine.assemble_coach_context()
            ctx['has_gemini_key'] = bool(coach_engine.get_gemini_api_key())
            ctx['has_claude_key'] = bool(coach_engine.get_claude_api_key())
            ctx['has_api_key'] = ctx['has_gemini_key'] or ctx['has_claude_key']
            self.send_json(ctx)
        elif p == '/api/coach/history':
            coach_engine.init_coach_db()
            with db_conn() as c:
                msgs_desc = [dict(r) for r in c.execute('SELECT id, role, content, proposed_json, applied, created_at FROM coach_messages ORDER BY id DESC LIMIT 100').fetchall()]
                msgs = list(reversed(msgs_desc))
                mems = [dict(r) for r in c.execute('SELECT id, category, content, active, created_at FROM coach_memory ORDER BY category, id').fetchall()]
            has_gemini = bool(coach_engine.get_gemini_api_key())
            has_claude = bool(coach_engine.get_claude_api_key())
            self.send_json({'messages': msgs, 'memories': mems, 'has_api_key': has_gemini or has_claude, 'has_gemini_key': has_gemini, 'has_claude_key': has_claude})
        elif p == '/api/coach/memory':
            coach_engine.init_coach_db()
            with db_conn() as c:
                mems = [dict(r) for r in c.execute('SELECT id, category, content, active, created_at FROM coach_memory ORDER BY category, id').fetchall()]
            self.send_json({'memories': mems})
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
            write_snapshot()
            self.send_json({'ok': True})
        elif p == '/api/settings':
            save_settings(body)
            write_snapshot()
            self.send_json({'ok': True})
        elif p == '/api/import':
            replace_all(body)
            write_snapshot()
            self.send_json({'ok': True})
        elif p == '/api/sync-strava':
            result = strava_sync()
            write_snapshot()
            self.send_json(result)
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
        elif p == '/api/coach/chat':
            coach_engine.init_coach_db()
            msg = body.get('message', '').strip()
            if not msg:
                return self.send_json({'ok': False, 'error': 'Message required'}, 400)
            
            model = body.get('model') or 'gemini-3.7-flash'
            api_key = body.get('apiKey') or None  # let query_coach resolve from DB/env

            # Fetch prior history before saving current user message
            with db_conn() as c:
                hist_rows = c.execute('SELECT role, content FROM coach_messages ORDER BY id DESC LIMIT 10').fetchall()
                hist = [dict(r) for r in hist_rows]
                hist.reverse()

                # Save user message to database
                c.execute('INSERT INTO coach_messages (role, content, created_at) VALUES (?,?,?)',
                          ('user', msg, datetime.now().isoformat()))
                c.commit()

            context = coach_engine.assemble_coach_context()
            res = coach_engine.query_coach(msg, context, history_messages=hist, model=model, api_key=api_key)
            
            if res.get('ok'):
                prop_json = json.dumps(res.get('proposed_workouts', [])) if res.get('proposed_workouts') else None
                with db_conn() as c:
                    c.execute('INSERT INTO coach_messages (role, content, proposed_json, created_at) VALUES (?,?,?,?)',
                              ('model', res.get('coach_response', ''), prop_json, datetime.now().isoformat()))
                    c.commit()
            self.send_json(res)

        elif p == '/api/coach/apply':
            coach_engine.init_coach_db()
            workouts = body.get('proposed_workouts', [])
            new_memories = body.get('new_memories', [])
            msg_id = body.get('message_id')
            with db_conn() as c:
                for w in workouts:
                    d = w.get('date')
                    action = w.get('action', 'update').lower()
                    if action == 'delete':
                        c.execute('DELETE FROM workouts WHERE date=?', (d,))
                    else:
                        c.execute(
                            'INSERT OR REPLACE INTO workouts (date, name, tss, duration_minutes, notes) VALUES (?,?,?,?,?)',
                            (d, w.get('name',''), float(w.get('tss',0)), int(w.get('duration_minutes',0)), w.get('notes',''))
                        )
                now_iso = datetime.now().isoformat()
                for mem in new_memories:
                    c.execute('INSERT INTO coach_memory (category, content, active, created_at) VALUES (?,?,?,?)',
                              (mem.get('category','rule'), mem.get('content',''), 1, now_iso))
                if msg_id:
                    c.execute('UPDATE coach_messages SET applied=1 WHERE id=?', (msg_id,))
                c.commit()
            write_snapshot()
            self.send_json({'ok': True, 'data': load_all()})

        elif p == '/api/coach/memory':
            coach_engine.init_coach_db()
            cat = body.get('category', 'rule')
            content = body.get('content', '').strip()
            if not content:
                return self.send_json({'error': 'content required'}, 400)
            with db_conn() as c:
                c.execute('INSERT INTO coach_memory (category, content, active, created_at) VALUES (?,?,?,?)',
                          (cat, content, 1, datetime.now().isoformat()))
                c.commit()
            self.send_json({'ok': True})

        elif p == '/api/coach/memory/toggle':
            mem_id = body.get('id')
            active = body.get('active', 1)
            with db_conn() as c:
                c.execute('UPDATE coach_memory SET active=? WHERE id=?', (active, mem_id))
                c.commit()
            self.send_json({'ok': True})

        elif p == '/api/coach/clear-chat':
            with db_conn() as c:
                c.execute('DELETE FROM coach_messages')
                c.commit()
            self.send_json({'ok': True})

        elif p == '/api/garmin/push':
            date = body.get('date')
            if not date:
                return self.send_json({'ok': False, 'error': 'date is required'}, 400)

            name = body.get('name')
            notes = body.get('notes')
            duration_minutes = body.get('duration_minutes')

            # Fetch planned workout from DB if not fully provided in body
            with db_conn() as c:
                row = c.execute('SELECT name, notes, duration_minutes, tss FROM workouts WHERE date=?', (date,)).fetchone()
                if row:
                    name = name or row['name']
                    notes = notes or row['notes']
                    if duration_minutes is None:
                        duration_minutes = row['duration_minutes']

            if not name:
                return self.send_json({'ok': False, 'error': f'No planned workout found for {date}'}, 404)

            try:
                import push_garmin_workout
                res = push_garmin_workout.push_workout(name, date, notes=notes, duration_minutes=duration_minutes)
                status_code = 200 if res.get('ok') else 400
                self.send_json(res, status_code)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)

        elif p == '/api/zwift/push':
            date = body.get('date')
            if not date:
                return self.send_json({'ok': False, 'error': 'date is required'}, 400)

            name = body.get('name')
            notes = body.get('notes')
            duration_minutes = body.get('duration_minutes')

            # Fetch planned workout and athlete FTP from DB
            with db_conn() as c:
                row = c.execute('SELECT name, notes, duration_minutes, tss FROM workouts WHERE date=?', (date,)).fetchone()
                if row:
                    name = name or row['name']
                    notes = notes or row['notes']
                    if duration_minutes is None:
                        duration_minutes = row['duration_minutes']

                ftp_row = c.execute("SELECT value FROM settings WHERE key='ftp'").fetchone()
                ftp = 278.0
                if ftp_row and ftp_row['value']:
                    try:
                        ftp = float(json.loads(ftp_row['value']))
                    except Exception:
                        try: ftp = float(ftp_row['value'])
                        except Exception: pass

            if not name:
                return self.send_json({'ok': False, 'error': f'No planned workout found for {date}'}, 404)

            try:
                import zwift_workout
                res = zwift_workout.push_zwift_workout(name, date, notes=notes, duration_minutes=duration_minutes, ftp=ftp)
                status_code = 200 if res.get('ok') else 400
                self.send_json(res, status_code)
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)}, 500)

        else:
            self.send_json({'error': 'not found'}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith('/api/workout/'):
            delete_workout(p.rsplit('/', 1)[-1])
            self.send_json({'ok': True})
        elif p.startswith('/api/coach/memory/'):
            mem_id = p.rsplit('/', 1)[-1]
            with db_conn() as c:
                c.execute('DELETE FROM coach_memory WHERE id=?', (mem_id,))
                c.commit()
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
        write_snapshot()
        print(f'    Snapshot → training_snapshot.json updated')

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
