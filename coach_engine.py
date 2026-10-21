#!/usr/bin/env python3
"""
Virtual Cycling Coach Agent Engine for Form Forward.
Handles:
1. Deterministic context assembly (date grid, PMC metrics, recent compliance, upcoming plan, coach memory)
2. Gemini API integration with structured function calling
3. Mathematical and date guardrails for workout proposals
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import date as dt_date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'training.db')

BIKE_SPORTS = {
    'Ride', 'VirtualRide', 'GravelRide', 'MountainBikeRide',
    'EBikeRide', 'EMountainBikeRide', 'Velomobile',
}

RUN_SPORTS = {
    'Run', 'TrailRun', 'VirtualRun',
}

def calc_activity_tss(row, ftp=278.0, lthr=165.0):
    """Estimate TSS for one strava_activities row."""
    sport = row['sport_type'] or ''
    move_secs = row['moving_time'] or 0
    if move_secs <= 0:
        return 0.0

    if sport in BIKE_SPORTS:
        watts = 0.0
        if row['device_watts'] and row['weighted_average_watts'] and row['weighted_average_watts'] > 0:
            watts = float(row['weighted_average_watts'])       # NP from power meter (best)
        elif row['device_watts'] and row['average_watts'] and row['average_watts'] > 0:
            watts = float(row['average_watts'])                # avg watts from power meter

        if watts > 0 and ftp > 0:
            return (move_secs * watts * watts) / (ftp * ftp * 3600) * 100
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    elif sport in RUN_SPORTS:
        avg_hr = row['average_heartrate'] if row['average_heartrate'] else None
        if avg_hr and avg_hr > 0 and lthr > 0:
            hr_ratio = float(avg_hr) / float(lthr)
            return (move_secs / 3600.0) * (hr_ratio ** 2) * 100
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    else:
        if row['suffer_score'] and row['suffer_score'] > 0:
            return float(row['suffer_score'])

    return 0.0

# ── Coach Memory Seed Data (default rules, seeded once on first run) ───────────

INITIAL_MEMORIES = [
    ("rule", "Schedule: Monday and Wednesday are mandatory rest days (0 TSS). Do not schedule intensity or Zone 2 on these days without explicit athlete permission."),
    ("rule", "Schedule: Tuesday and Thursday are key weekday quality sessions (60 min duration, target TSS ~80-85, IF ~0.88-0.90)."),
    ("rule", "Schedule: Saturday and Sunday are weekend rides / races. Sunday outdoor long rides typically generate ~180-205 TSS (3.5-4 hours)."),
    ("rule", "Workout Descriptions: Every workout description MUST account for 100% of the total duration (Warmup + all intervals + all recoveries + cooldown = duration_minutes). Format: [warmup] | [main set] | [cooldown] | [notes]."),
    ("goal", "Target: Maintain progressive overload in Build phases, with CTL target ~45-48 and positive TSB (+7 to +12) on key race mornings."),
    ("observation", "Outdoor rides run hotter than Zwift/indoor rides; adjust TSS targets accordingly for outdoor long rides."),
]

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_coach_db():
    with db_conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS coach_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS coach_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            proposed_json TEXT,
            applied INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )''')
        # Seed memory if empty
        row = c.execute('SELECT COUNT(*) as cnt FROM coach_memory').fetchone()
        if row['cnt'] == 0:
            now_iso = datetime.now().isoformat()
            for cat, content in INITIAL_MEMORIES:
                c.execute('INSERT INTO coach_memory (category, content, active, created_at) VALUES (?,?,?,?)',
                          (cat, content, 1, now_iso))
        c.commit()

# ── Context Assembler ──────────────────────────────────────────────────────────

def get_claude_api_key():
    key = os.environ.get('ANTHROPIC_API_KEY')
    if key:
        return key.strip()
    env_file = os.path.join(BASE, '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    if line.startswith('ANTHROPIC_API_KEY='):
                        return line.split('=', 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    try:
        with db_conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key='claude_api_key'").fetchone()
            if row and row['value']:
                try:
                    val = json.loads(row['value'])
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                except Exception:
                    return str(row['value']).strip()
    except Exception:
        pass
    return None


def get_gemini_api_key():
    # 1. Environment variable
    key = os.environ.get('GEMINI_API_KEY')
    if key:
        return key.strip()
    
    # 2. .env file
    env_file = os.path.join(BASE, '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    if line.startswith('GEMINI_API_KEY='):
                        return line.split('=', 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass

    # 3. Settings table in DB
    try:
        with db_conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key='gemini_api_key'").fetchone()
            if row and row['value']:
                try:
                    val = json.loads(row['value'])
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                except Exception:
                    return str(row['value']).strip()
    except Exception:
        pass

    return None

def assemble_coach_context():
    """Builds a rich, zero-ambiguity context payload for the Coach Agent."""
    init_coach_db()
    today = dt_date.today()
    today_iso = today.isoformat()
    today_dayname = today.strftime('%A')
    
    with db_conn() as c:
        # 1. Settings
        settings_rows = c.execute('SELECT key, value FROM settings').fetchall()
        settings = {}
        for r in settings_rows:
            try:
                settings[r['key']] = json.loads(r['value'])
            except Exception:
                settings[r['key']] = r['value']
                
        # 2. Planned workouts (next 60 days)
        window_end = (today + timedelta(days=60)).isoformat()
        planned_rows = c.execute(
            'SELECT date, name, tss, duration_minutes, notes FROM workouts WHERE date >= ? AND date <= ? ORDER BY date',
            ((today - timedelta(days=7)).isoformat(), window_end)
        ).fetchall()
        planned_dict = {}
        for r in planned_rows:
            planned_dict[r['date']] = {
                'name': r['name'],
                'tss': r['tss'],
                'duration_minutes': r['duration_minutes'],
                'notes': r['notes']
            }
            
        # 3. Completed rides (last 28 days)
        since_28d = (today - timedelta(days=28)).isoformat()
        act_rows = c.execute('''
            SELECT a.id, a.start_date_local_date, a.start_date_local, a.name, a.sport_type,
                   a.moving_time, a.distance, a.total_elevation_gain,
                   a.device_watts, a.weighted_average_watts, a.average_watts,
                   a.average_heartrate, a.max_heartrate, a.suffer_score,
                   s.workout_type, s.quality_notes
            FROM strava_activities a
            LEFT JOIN activity_streams s ON s.activity_id = a.id
            WHERE a.start_date_local_date >= ?
            ORDER BY a.start_date_local DESC
        ''', (since_28d,)).fetchall()

        # Load manual TSS overrides
        overrides = {}
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tss_overrides' in tables:
            overrides = {r['date']: r['tss'] for r in c.execute('SELECT date, tss FROM tss_overrides')}
        
        # 4. Phases & Races
        phases = [dict(r) for r in c.execute('SELECT start_date, end_date, name FROM phases ORDER BY start_date').fetchall()]
        races = [dict(r) for r in c.execute("SELECT date, name, notes FROM workouts WHERE name LIKE '%🏁%' OR notes LIKE '%race%' ORDER BY date").fetchall()]
        
        # 5. Coach Memory
        memories = [dict(r) for r in c.execute('SELECT id, category, content FROM coach_memory WHERE active=1 ORDER BY category, id').fetchall()]

    ftp = float(settings.get('ftp', 278))
    lthr = float(settings.get('lthr', 165))

    # Format calendar grid for upcoming 4 weeks
    upcoming_calendar = []
    for i in range(0, 35):
        d = today + timedelta(days=i)
        d_iso = d.isoformat()
        d_name = d.strftime('%A')
        w = planned_dict.get(d_iso, {})
        upcoming_calendar.append({
            'date': d_iso,
            'day_of_week': d_name,
            'is_today': (i == 0),
            'planned_name': w.get('name', 'Rest / Off'),
            'planned_tss': w.get('tss', 0),
            'planned_duration': w.get('duration_minutes', 0),
            'notes': w.get('notes', '')
        })

    # Recent activities summary with computed TSS and Intensity Factor
    recent_activities = []
    for r in act_rows:
        moving_min = round((r['moving_time'] or 0) / 60, 1)
        act_tss = round(calc_activity_tss(r, ftp, lthr), 1)
        np_w = round(r['weighted_average_watts']) if r['weighted_average_watts'] else None
        avg_w = round(r['average_watts']) if r['average_watts'] else None
        if_val = round(np_w / ftp, 2) if (np_w and ftp > 0) else None
        recent_activities.append({
            'date': r['start_date_local_date'],
            'name': r['name'],
            'sport': r['sport_type'],
            'duration_min': moving_min,
            'tss': act_tss,
            'avg_watts': avg_w,
            'np_watts': np_w,
            'intensity_factor': if_val,
            'avg_hr': round(r['average_heartrate']) if r['average_heartrate'] else None,
            'max_hr': round(r['max_heartrate']) if r['max_heartrate'] else None,
            'suffer_score': r['suffer_score'],
            'quality_notes': r['quality_notes']
        })

    # ── 14-Day Detailed Completed Workload & TSS ──────────────────────────────
    start_14d = today - timedelta(days=13)
    acts_by_date = defaultdict(list)
    for r in act_rows:
        if r['start_date_local_date'] >= start_14d.isoformat():
            acts_by_date[r['start_date_local_date']].append(r)

    last_14_days_training = []
    summary_lines = []

    total_14d_tss = 0.0
    total_14d_min = 0.0
    total_14d_rides = 0

    past_7d_tss = 0.0
    past_7d_min = 0.0
    prior_7d_tss = 0.0
    prior_7d_min = 0.0

    hardest_ride = None
    hardest_ride_tss = 0.0

    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        d_iso = d.isoformat()
        d_name = d.strftime('%A')
        d_short = f"{d.strftime('%a, %b')} {d.day}"
        is_today = (i == 0)

        day_acts = acts_by_date.get(d_iso, [])
        planned = planned_dict.get(d_iso, {})

        if d_iso in overrides:
            day_tss = float(overrides[d_iso])
            is_override = True
        else:
            day_tss = sum(calc_activity_tss(a, ftp, lthr) for a in day_acts)
            is_override = False

        day_min = sum((a['moving_time'] or 0) / 60.0 for a in day_acts)
        total_14d_tss += day_tss
        total_14d_min += day_min
        total_14d_rides += len(day_acts)

        if i < 7:
            past_7d_tss += day_tss
            past_7d_min += day_min
        else:
            prior_7d_tss += day_tss
            prior_7d_min += day_min

        act_details = []
        act_text_parts = []
        for a in day_acts:
            act_tss = round(calc_activity_tss(a, ftp, lthr), 1)
            if act_tss > hardest_ride_tss:
                hardest_ride_tss = act_tss
                hardest_ride = f"\"{a['name']}\" on {d_short} ({act_tss} TSS, {round((a['moving_time'] or 0)/60)} min)"

            np_w = round(a['weighted_average_watts']) if a['weighted_average_watts'] else None
            avg_w = round(a['average_watts']) if a['average_watts'] else None
            if_val = round(np_w / ftp, 2) if (np_w and ftp > 0) else None
            avg_hr = round(a['average_heartrate']) if a['average_heartrate'] else None
            max_hr = round(a['max_heartrate']) if a['max_heartrate'] else None
            mov_min = round((a['moving_time'] or 0) / 60.0, 1)

            act_details.append({
                'name': a['name'],
                'sport': a['sport_type'],
                'tss': act_tss,
                'moving_minutes': mov_min,
                'np_watts': np_w,
                'avg_watts': avg_w,
                'intensity_factor': if_val,
                'avg_hr': avg_hr,
                'max_hr': max_hr,
                'quality_notes': a['quality_notes']
            })

            metrics = [f"{mov_min}m", f"{act_tss} TSS"]
            if np_w: metrics.append(f"NP {np_w}W")
            if if_val: metrics.append(f"IF {if_val}")
            if avg_hr: metrics.append(f"HR {avg_hr}")
            act_text_parts.append(f"\"{a['name']}\" ({' | '.join(metrics)})")

        is_rest = (len(day_acts) == 0 and day_tss <= 5)
        day_record = {
            'date': d_iso,
            'day_of_week': d_name,
            'day_short': d_short,
            'is_today': is_today,
            'is_rest_day': is_rest,
            'total_tss': round(day_tss, 1),
            'total_moving_minutes': round(day_min, 1),
            'total_moving_hours': round(day_min / 60.0, 2),
            'is_manual_override': is_override,
            'planned_workout': planned.get('name') if planned else None,
            'planned_tss': planned.get('tss', 0) if planned else 0,
            'activities': act_details
        }
        last_14_days_training.append(day_record)

        prefix = f"• {d_name}, {d.strftime('%b')} {d.day}: {round(day_tss, 1)} TSS"
        if is_today:
            prefix += " [TODAY]"
        if is_rest:
            summary_lines.append(f"{prefix} — Rest / Off (0 TSS)")
        else:
            summary_lines.append(f"{prefix} ({round(day_min, 1)} min) — {'; '.join(act_text_parts)}")

    last_14_days_summary = {
        'last_7_days_total_tss': round(past_7d_tss, 1),
        'last_7_days_hours': round(past_7d_min / 60.0, 1),
        'prior_7_days_total_tss': round(prior_7d_tss, 1),
        'prior_7_days_hours': round(prior_7d_min / 60.0, 1),
        'two_week_total_tss': round(total_14d_tss, 1),
        'two_week_total_hours': round(total_14d_min / 60.0, 1),
        'two_week_ride_count': total_14d_rides,
        'daily_avg_tss': round(total_14d_tss / 14.0, 1),
        'hardest_ride': hardest_ride
    }

    last_14_days_text_summary = "================================================================================\n"
    last_14_days_text_summary += f"ATHLETE COMPLETED WORKLOAD & TSS (LAST 14 DAYS — Ending {today_iso}):\n"
    last_14_days_text_summary += "================================================================================\n"
    last_14_days_text_summary += "\n".join(summary_lines) + "\n"
    last_14_days_text_summary += "--------------------------------------------------------------------------------\n"
    last_14_days_text_summary += "14-DAY WORKLOAD SUMMARY:\n"
    last_14_days_text_summary += f"• Past 7 Days (Days 0-6): {last_14_days_summary['last_7_days_total_tss']} TSS across {last_14_days_summary['last_7_days_hours']} hrs (Daily avg: {round(past_7d_tss/7.0, 1)} TSS)\n"
    last_14_days_text_summary += f"• Prior 7 Days (Days 7-13): {last_14_days_summary['prior_7_days_total_tss']} TSS across {last_14_days_summary['prior_7_days_hours']} hrs (Daily avg: {round(prior_7d_tss/7.0, 1)} TSS)\n"
    last_14_days_text_summary += f"• 14-Day Cumulative: {last_14_days_summary['two_week_total_tss']} TSS across {last_14_days_summary['two_week_total_hours']} hrs ({last_14_days_summary['two_week_ride_count']} rides)\n"
    if hardest_ride:
        last_14_days_text_summary += f"• Hardest Workout: {hardest_ride}\n"
    last_14_days_text_summary += "================================================================================\n"

    # Calculate daily completed TSS history to compute current Acute Recovery Balance (ACB)
    history_tss_days = {}
    with db_conn() as c:
        act_all = c.execute('''
            SELECT start_date_local_date, moving_time, sport_type, device_watts,
                   average_watts, weighted_average_watts, average_heartrate, suffer_score
            FROM strava_activities
            WHERE start_date_local_date >= ?
        ''', ((today - timedelta(days=60)).isoformat(),)).fetchall()

    for r in act_all:
        d = r['start_date_local_date']
        if d in overrides:
            history_tss_days[d] = float(overrides[d])
        else:
            history_tss_days[d] = history_tss_days.get(d, 0.0) + calc_activity_tss(r, ftp, lthr)

    # Compute continuous ACB curve up to today
    deficit = 0.0
    days_since_rest = 0
    recent_window = []
    hole_decay = 0.78 # 4-day washout half-life

    for i in range(60, -1, -1):
        d_check = (today - timedelta(days=i)).isoformat()
        tss_day = history_tss_days.get(d_check, 0.0)

        if tss_day <= 10:
            days_since_rest = 0
        else:
            days_since_rest += 1

        recent_window.append(tss_day)
        if len(recent_window) > 7:
            recent_window.pop(0)
        avg7 = sum(recent_window) / len(recent_window)
        base = max(35.0, avg7)

        if tss_day > base:
            excess = tss_day - base
            spike = (tss_day / base) ** 0.3
            deficit = (deficit * hole_decay) - (excess * spike)
        elif tss_day <= 15:
            gain = max(10.0, 45.0 - (tss_day * 1.2))
            deficit = (deficit * hole_decay) + gain
        else:
            drag = max(0, days_since_rest - 3) * 3.5
            deficit = (deficit * hole_decay) - drag
        deficit = min(15.0, deficit)

    current_acb = round(deficit, 1)
    acb_status = "🟢 FRESH / RECOVERED (≥0)" if current_acb >= 0 else ("🟡 MODERATE STRAIN (-25 to 0)" if current_acb > -25 else "🔴 DEEP FATIGUE HOLE (< -25 TSS)")

    return {
        'today': today_iso,
        'day_of_week': today_dayname,
        'current_acute_recovery_balance': current_acb,
        'acute_recovery_status': acb_status,
        'days_without_true_rest': days_since_rest,
        'athlete_settings': settings,
        'active_memories': memories,
        'phases': phases,
        'target_races': races,
        'upcoming_calendar': upcoming_calendar,
        'last_14_days_training': last_14_days_training,
        'last_14_days_summary': last_14_days_summary,
        'last_14_days_text_summary': last_14_days_text_summary,
        'recent_activities': recent_activities[:20]
    }

# ── Validation Guardrails ──────────────────────────────────────────────────────

def validate_proposals(proposals, today_iso):
    """Validates proposed workout updates for correct dates and structure."""
    errors = []
    validated = []
    
    for p in proposals:
        d_str = p.get('date')
        if not d_str:
            errors.append("Workout proposal missing date.")
            continue
        try:
            d_obj = dt_date.fromisoformat(d_str)
        except ValueError:
            errors.append(f"Invalid date format: {d_str}. Must be YYYY-MM-DD.")
            continue
            
        action = p.get('action', 'update').lower()
        name = p.get('name', '').strip()
        tss = float(p.get('tss', 0))
        duration = int(p.get('duration_minutes', 0))
        notes = p.get('notes', '').strip()
        reason = p.get('reason', '').strip()
        
        validated.append({
            'date': d_str,
            'day_of_week': d_obj.strftime('%A'),
            'action': action,
            'name': name,
            'tss': tss,
            'duration_minutes': duration,
            'notes': notes,
            'reason': reason
        })
        
    return validated, errors

# ── Gemini & Claude API Clients ───────────────────────────────────────────────

SYSTEM_INSTRUCTION = """You are "Coach" — an elite, highly analytical virtual cycling coach for the Form Forward training app.
You manage the athlete's Performance Management Chart (PMC) and Acute Recovery Balance (ACB / "The Recovery Hole").

### Core Coaching Directives:
1. DETERMINISTIC DATES: Today is {today} ({day_of_week}). Always reference exact calendar dates (YYYY-MM-DD) and matching days of week. Never guess or shift dates accidentally.
2. SCHEDULE RULES & CONSTRAINTS:
   - Monday and Wednesday: Mandatory Rest Days (0 TSS). Do NOT schedule intensity or endurance rides on Mon/Wed without explicit instructions.
   - Tuesday and Thursday: Key weekday interval quality sessions (~60 min, target TSS ~80-85, IF ~0.88-0.90).
   - Sunday: Long outdoor ride (~180-205 TSS, 3.5-4 hours).
3. RECENT WORKLOAD & 14-DAY TSS AWARENESS:
   - You are provided with the athlete's complete completed workload and day-by-day TSS for the last 14 days, as well as rolling 7-day volume totals, daily averages, and hardest rides.
   - ALWAYS evaluate recent fatigue, recovery, and readiness using these exact TSS values and Intensity Factors (IF).
   - When the athlete asks how hard recent workouts were, how much training stress they accumulated, or whether they should push harder or rest, cite their specific recent TSS numbers, ride names, and weekly training load from this 14-day history.
4. ACUTE RECOVERY BALANCE & THE "TOUCH GREEN" RULE:
   - Current State: Athlete's Acute Recovery Balance is {current_acb} TSS ({acb_status}) with {days_since_rest} consecutive days without true rest (<=10 TSS).
   - The Sweet Spot: The athlete should train primarily in the Yellow Zone (-5 to -25 TSS ACB) to drive progressive overload.
   - Mandatory Weekly Supercompensation: The athlete MUST touch the Green Zone (≥ 0 TSS ACB) at least once every 7-day microcycle (on scheduled rest days <= 10 TSS).
   - RED HOLE ALERT (< -25 TSS): If the athlete is submerged in the Red Zone or has gone >4 days without a true rest day, you MUST NOT schedule high-intensity workouts. You must prescribe true rest (0-10 TSS) or light active recovery until the deficit clears back into the yellow/green.
5. WORKOUT STRUCTURE:
   - Every workout notes/description must account for 100% of the duration: [warmup] | [main set] | [cooldown] | [context].
6. FUNCTION CALLING:
   - Whenever you recommend modifying, creating, or deleting planned workouts, you MUST call the `propose_plan_updates` tool with the exact structured updates.
   - If the athlete shares a permanent rule, injury, or schedule constraint, include it in `new_memory_items`.
"""

TOOLS_DEFINITION = [
    {
        "function_declarations": [
            {
                "name": "propose_plan_updates",
                "description": "Propose structured updates, additions, or deletions to upcoming planned workouts in the training calendar.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "coach_response": {
                            "type": "STRING",
                            "description": "Coaching rationale and conversational explanation for the athlete."
                        },
                        "new_memory_items": {
                            "type": "ARRAY",
                            "description": "New long-term coaching rules, injury notes, or athlete preferences to store in persistent memory.",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "category": {
                                        "type": "STRING",
                                        "enum": ["rule", "goal", "injury", "observation", "preference"]
                                    },
                                    "content": {
                                        "type": "STRING",
                                        "description": "The persistent rule or memory note."
                                    }
                                },
                                "required": ["category", "content"]
                            }
                        },
                        "workout_updates": {
                            "type": "ARRAY",
                            "description": "List of planned workouts to modify, create, or delete.",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "date": {
                                        "type": "STRING",
                                        "description": "ISO date YYYY-MM-DD for the workout"
                                    },
                                    "action": {
                                        "type": "STRING",
                                        "enum": ["update", "create", "delete"]
                                    },
                                    "name": {
                                        "type": "STRING",
                                        "description": "Short workout title e.g. '5x5 VO2 Max' or 'Rest Day'"
                                    },
                                    "tss": {
                                        "type": "NUMBER",
                                        "description": "Target Training Stress Score (TSS)"
                                    },
                                    "duration_minutes": {
                                        "type": "INTEGER",
                                        "description": "Total duration in minutes"
                                    },
                                    "notes": {
                                        "type": "STRING",
                                        "description": "Structured interval breakdown: [warmup] | [main set] | [cooldown]"
                                    },
                                    "reason": {
                                        "type": "STRING",
                                        "description": "Specific coaching reason for this day's adjustment"
                                    }
                                },
                                "required": ["date", "action", "name", "tss", "duration_minutes", "notes"]
                            }
                        }
                    },
                    "required": ["coach_response", "workout_updates"]
                }
            }
        ]
    }
]

MODEL_CASCADE = [
    'gemini-3.7-flash',
    'gemini-3.8-flash',
    'gemini-3.5-flash',
    'gemini-flash-latest',
    'gemini-3.5-flash-lite',
]

CLAUDE_MODELS = [
    'claude-sonnet-4-6',
    'claude-opus-5',
    'claude-fable-5',
    'claude-haiku-4-5-20251001',
]

# Claude tool definition (input_schema format, lowercase types)
CLAUDE_TOOLS = [
    {
        "name": "propose_plan_updates",
        "description": "Propose structured updates, additions, or deletions to upcoming planned workouts in the training calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "coach_response": {
                    "type": "string",
                    "description": "Coaching rationale and conversational explanation for the athlete."
                },
                "new_memory_items": {
                    "type": "array",
                    "description": "New long-term coaching rules, injury notes, or athlete preferences to store in persistent memory.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "enum": ["rule", "goal", "injury", "observation", "preference"]},
                            "content": {"type": "string", "description": "The persistent rule or memory note."}
                        },
                        "required": ["category", "content"]
                    }
                },
                "workout_updates": {
                    "type": "array",
                    "description": "List of planned workouts to modify, create, or delete.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                            "action": {"type": "string", "enum": ["update", "create", "delete"]},
                            "name": {"type": "string"},
                            "tss": {"type": "number"},
                            "duration_minutes": {"type": "integer"},
                            "notes": {"type": "string", "description": "Structured interval breakdown: [warmup] | [main set] | [cooldown]"},
                            "reason": {"type": "string"}
                        },
                        "required": ["date", "action", "name", "tss", "duration_minutes", "notes"]
                    }
                }
            },
            "required": ["coach_response", "workout_updates"]
        }
    }
]

def query_gemini(api_key, user_message, context, history_messages=None, model=None):
    """
    Calls Gemini API with automatic model failover cascade to ensure zero-downtime
    when individual model endpoints experience high demand or 503/429 errors.
    """
    models_to_try = [model] if model else list(MODEL_CASCADE)
    for m in MODEL_CASCADE:
        if m not in models_to_try:
            models_to_try.append(m)

    # Build context string
    system_text = SYSTEM_INSTRUCTION.format(
        today=context['today'],
        day_of_week=context['day_of_week'],
        current_acb=context.get('current_acute_recovery_balance', 0),
        acb_status=context.get('acute_recovery_status', 'Unknown'),
        days_since_rest=context.get('days_without_true_rest', 0)
    )
    
    context_str = json.dumps({
        "today": f"{context['today']} ({context['day_of_week']})",
        "current_acute_recovery_balance": context.get('current_acute_recovery_balance', 0),
        "acute_recovery_status": context.get('acute_recovery_status', 'Unknown'),
        "days_without_true_rest": context.get('days_without_true_rest', 0),
        "athlete_settings": context['athlete_settings'],
        "persistent_rules_and_memory": [m['content'] for m in context['active_memories']],
        "active_phases": context['phases'],
        "target_races": context['target_races'],
        "completed_training_and_tss_last_14_days": context.get('last_14_days_training', []),
        "recent_14_day_workload_summary": context.get('last_14_days_summary', {}),
        "recent_completed_activities_last_28d": context['recent_activities'],
        "upcoming_planned_calendar": context['upcoming_calendar']
    }, indent=2)

    text_summary = context.get('last_14_days_text_summary', '')

    # Build conversation contents
    contents = []
    
    # System / Context introduction as first turn
    contents.append({
        "role": "user",
        "parts": [{"text": f"SYSTEM & ATHLETE CONTEXT:\n{context_str}\n\n{text_summary}\n\nPlease acknowledge and wait for instructions."}]
    })
    contents.append({
        "role": "model",
        "parts": [{"text": f"Understood. I have loaded your full training state as of {context['today']} ({context['day_of_week']}), your completed workload and day-by-day TSS for the last 14 days, your active phases, recent completed Strava rides, upcoming plan, and all persistent rules. How can I help you optimize your training today?"}]
    })
    
    # Append recent history
    if history_messages:
        for msg in history_messages[-6:]:
            role = "user" if msg['role'] == 'user' else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg['content']}]
            })
            
    # Append current user message
    contents.append({
        "role": "user",
        "parts": [{"text": user_message}]
    })

    payload = {
        "system_instruction": {
            "parts": [{"text": system_text}]
        },
        "contents": contents,
        "tools": TOOLS_DEFINITION,
        "tool_config": {
            "function_calling_config": {
                "mode": "AUTO"
            }
        }
    }

    req_data = json.dumps(payload).encode('utf-8')
    last_error = ""

    for target_model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        for attempt in range(2):  # 2 attempts per model in cascade
            try:
                with urllib.request.urlopen(req, timeout=35) as response:
                    res_body = response.read().decode('utf-8')
                    res_json = json.loads(res_body)
                    
                    candidates = res_json.get('candidates', [])
                    if not candidates:
                        last_error = f"No response generated from {target_model}."
                        break
                    
                    content_part = candidates[0].get('content', {}).get('parts', [])
                    
                    coach_text = ""
                    proposed_workouts = []
                    new_memories = []
                    
                    for part in content_part:
                        if 'text' in part:
                            coach_text += part['text']
                        if 'functionCall' in part:
                            fn = part['functionCall']
                            if fn.get('name') == 'propose_plan_updates':
                                args = fn.get('args', {})
                                if args.get('coach_response'):
                                    coach_text = args['coach_response']
                                proposed_workouts = args.get('workout_updates', [])
                                new_memories = args.get('new_memory_items', [])

                    # Validate workouts
                    validated_workouts, val_errors = validate_proposals(proposed_workouts, context['today'])
                    
                    return {
                        'ok': True,
                        'model_used': target_model,
                        'coach_response': coach_text.strip(),
                        'proposed_workouts': validated_workouts,
                        'new_memories': new_memories,
                        'validation_errors': val_errors
                    }
                    
            except urllib.error.HTTPError as e:
                err_msg = e.read().decode('utf-8')
                err_text = ""
                try:
                    err_json = json.loads(err_msg)
                    err_text = err_json.get('error', {}).get('message', err_msg)
                except Exception:
                    err_text = err_msg

                last_error = f"{target_model}: {err_text}"
                
                # Check if transient / high demand
                is_transient = e.code in (429, 500, 503) or 'high demand' in err_text.lower() or 'overloaded' in err_text.lower()
                if is_transient and attempt == 0:
                    time.sleep(1.5)
                    continue
                # If still failing, break inner loop to try next model in cascade
                break

            except Exception as e:
                last_error = f"{target_model}: {str(e)}"
                break

    return {'ok': False, 'error': f"Gemini API Error: {last_error}"}


def query_claude(api_key, user_message, context, history_messages=None, model=None):
    """Calls Anthropic Messages API with tool use for propose_plan_updates."""
    target_model = model if model in CLAUDE_MODELS else CLAUDE_MODELS[0]

    system_text = SYSTEM_INSTRUCTION.format(
        today=context['today'],
        day_of_week=context['day_of_week'],
        current_acb=context.get('current_acute_recovery_balance', 0),
        acb_status=context.get('acute_recovery_status', 'Unknown'),
        days_since_rest=context.get('days_without_true_rest', 0)
    )

    context_str = json.dumps({
        "today": f"{context['today']} ({context['day_of_week']})",
        "current_acute_recovery_balance": context.get('current_acute_recovery_balance', 0),
        "acute_recovery_status": context.get('acute_recovery_status', 'Unknown'),
        "days_without_true_rest": context.get('days_without_true_rest', 0),
        "athlete_settings": context['athlete_settings'],
        "persistent_rules_and_memory": [m['content'] for m in context['active_memories']],
        "active_phases": context['phases'],
        "target_races": context['target_races'],
        "completed_training_and_tss_last_14_days": context.get('last_14_days_training', []),
        "recent_14_day_workload_summary": context.get('last_14_days_summary', {}),
        "recent_completed_activities_last_28d": context['recent_activities'],
        "upcoming_planned_calendar": context['upcoming_calendar']
    }, indent=2)

    text_summary = context.get('last_14_days_text_summary', '')

    messages = []
    # Context primer turn
    messages.append({"role": "user", "content": f"SYSTEM & ATHLETE CONTEXT:\n{context_str}\n\n{text_summary}\n\nPlease acknowledge and wait for instructions."})
    messages.append({"role": "assistant", "content": f"Understood. I have loaded your full training state as of {context['today']} ({context['day_of_week']}), your completed workload and day-by-day TSS for the last 14 days, your active phases, recent completed Strava rides, upcoming plan, and all persistent rules. How can I help you optimize your training today?"})

    if history_messages:
        for msg in history_messages[-6:]:
            role = "user" if msg['role'] == 'user' else "assistant"
            messages.append({"role": role, "content": msg['content']})

    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": target_model,
        "max_tokens": 4096,
        "system": system_text,
        "tools": CLAUDE_TOOLS,
        "messages": messages
    }

    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            res_json = json.loads(response.read().decode('utf-8'))

        coach_text = ""
        proposed_workouts = []
        new_memories = []

        for block in res_json.get('content', []):
            if block.get('type') == 'text':
                coach_text += block.get('text', '')
            elif block.get('type') == 'tool_use' and block.get('name') == 'propose_plan_updates':
                args = block.get('input', {})
                if args.get('coach_response'):
                    coach_text = args['coach_response']
                proposed_workouts = args.get('workout_updates', [])
                new_memories = args.get('new_memory_items', [])

        validated_workouts, val_errors = validate_proposals(proposed_workouts, context['today'])
        return {
            'ok': True,
            'model_used': target_model,
            'coach_response': coach_text.strip(),
            'proposed_workouts': validated_workouts,
            'new_memories': new_memories,
            'validation_errors': val_errors
        }

    except urllib.error.HTTPError as e:
        err_text = e.read().decode('utf-8')
        try:
            err_text = json.loads(err_text).get('error', {}).get('message', err_text)
        except Exception:
            pass
        return {'ok': False, 'error': f"Claude API Error ({target_model}): {err_text}"}
    except Exception as e:
        return {'ok': False, 'error': f"Claude API Error: {str(e)}"}


def query_coach(user_message, context, history_messages=None, model=None, api_key=None):
    """Unified dispatcher — routes to Claude or Gemini based on model name."""
    if model and model.startswith('claude'):
        key = api_key or get_claude_api_key()
        if not key:
            return {'ok': False, 'error': 'Claude API key is not configured. Please enter it in Settings.'}
        return query_claude(key, user_message, context, history_messages=history_messages, model=model)
    else:
        key = api_key or get_gemini_api_key()
        if not key:
            return {'ok': False, 'error': 'Gemini API key is not configured. Please enter it in Settings.'}
        return query_gemini(key, user_message, context, history_messages=history_messages, model=model)


if __name__ == '__main__':
    ctx = assemble_coach_context()
    print(f"✅ Context assembled successfully for {ctx['today']} ({ctx['day_of_week']})")
    print(f"   Memories loaded: {len(ctx['active_memories'])}")
    print(f"   Upcoming days: {len(ctx['upcoming_calendar'])}")
    print(f"   Recent activities: {len(ctx['recent_activities'])}")
