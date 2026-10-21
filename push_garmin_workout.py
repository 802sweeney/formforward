#!/usr/bin/env python3
"""
Push a structured workout to Garmin Connect.
Requires a saved token from garmin_auth.py.

    python push_garmin_workout.py            # pushes today's sharpener
    python push_garmin_workout.py 2026-07-28 "2x10 low Z4"
    python push_garmin_workout.py --inspect  # print an existing workout's JSON
"""
import json
import os
import re
import sys
from datetime import date, datetime

import garth

TOKEN_DIR = os.path.expanduser("~/.garth")

# ── Step builders (format derived from Garmin Connect API inspection) ─────────

SPORT = {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2}
STROKE = {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0}
EQUIP  = {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0}

END_LAP  = {"conditionTypeId": 1, "conditionTypeKey": "lap.button",  "displayOrder": 1, "displayable": True}
END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time",        "displayOrder": 2, "displayable": True}

TGT_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",      "displayOrder": 1}
TGT_HR   = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone","displayOrder": 4}
TGT_PWR  = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone",     "displayOrder": 2}


def _step(order, type_id, type_key, end_cond, end_val, tgt, zone=None, desc=None):
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
        "childStepId": None,
        "description": desc,
        "endCondition": end_cond,
        "endConditionValue": float(end_val),
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "targetType": tgt,
        "targetValueOne": None,
        "targetValueTwo": None,
        "targetValueUnit": None,
        "zoneNumber": zone,
        "secondaryTargetType": None,
        "secondaryTargetValueOne": None,
        "secondaryTargetValueTwo": None,
        "secondaryTargetValueUnit": None,
        "secondaryZoneNumber": None,
        "endConditionZone": None,
        "strokeType": STROKE,
        "equipmentType": EQUIP,
        "category": None,
        "exerciseName": None,
        "workoutProvider": None,
        "providerExerciseSourceId": None,
        "weightValue": None,
        "weightUnit": None,
    }


def warmup(order, est_secs=900):
    """Lap-button warmup — user presses lap when ready."""
    return _step(order, 1, "warmup", END_LAP, est_secs, TGT_NONE)

def cooldown(order, est_secs=600):
    """Lap-button cooldown."""
    return _step(order, 2, "cooldown", END_LAP, est_secs, TGT_NONE)

def interval(order, secs, zone, power=False):
    """Timed interval — HR zone by default, power zone if power=True."""
    tgt = TGT_PWR if power else TGT_HR
    return _step(order, 3, "interval", END_TIME, secs, tgt, zone=zone)

def recovery(order, secs, zone=1, power=False):
    """Timed recovery — HR zone by default."""
    tgt = TGT_PWR if power else TGT_HR
    return _step(order, 4, "recovery", END_TIME, secs, tgt, zone=zone)

def repeat_group(order, reps, steps):
    """Repeat group — nested workoutSteps format."""
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "childStepId": None,
        "numberOfIterations": reps,
        "workoutSteps": steps,
    }


# ── Workout definitions ───────────────────────────────────────────────────────

WORKOUTS = {
    "Sharpener 6x45s Z5": {
        "name": "Sharpener 6×45s Z5",
        "desc": "15 min warmup → 6×45s all-out (Z5) with 3 min easy → cooldown",
        "steps": [
            warmup(1, 900),
            repeat_group(2, 6, [
                interval(1, 45,  5),
                recovery(2, 180, 1),
            ]),
            cooldown(3, 600),
        ],
    },
    "2x10 low Z4": {
        "name": "2×10 low Z4",
        "desc": "15 min warmup → 2×10 min threshold (Z4) with 5 min easy → cooldown",
        "steps": [
            warmup(1, 900),
            repeat_group(2, 2, [
                interval(1, 600, 4),
                recovery(2, 300, 2),
            ]),
            cooldown(3, 600),
        ],
    },
    "Sharpener 4-5x1m Z5": {
        "name": "Sharpener 4×1min Z5 (power)",
        "desc": "15 min warmup → 4×1min all-out (power zone 6) with 3 min easy → cooldown",
        "steps": [
            warmup(1, 900),
            repeat_group(2, 4, [
                interval(1, 60,  6, power=True),   # power zone 6 = ~121-150% FTP
                recovery(2, 180, 1),               # HR zone 1 recovery
            ]),
            cooldown(3, 600),
        ],
    },
    "Sharpener 3x1m Z5": {
        "name": "Sharpener 3×1min Z5 (power)",
        "desc": "15 min warmup → 3×1min all-out (power zone 6) with 3 min easy → cooldown",
        "steps": [
            warmup(1, 900),
            repeat_group(2, 3, [
                interval(1, 60,  6, power=True),
                recovery(2, 180, 1),
            ]),
            cooldown(3, 600),
        ],
    },
    "Race openers": {
        "name": "Race openers",
        "desc": "Easy Z2 with 3×20s explosive openers to prime the legs",
        "steps": [
            warmup(1, 1200),
            repeat_group(2, 3, [
                interval(1, 20,  5),
                recovery(2, 120, 1),
            ]),
            cooldown(3, 600),
        ],
    },
    "Race Sharpener — VO2 Punches": {
        "name": "Race Sharpener — VO2 Punches",
        "desc": "15 min warmup ramp 140→200W | 5x2 min @ 295-305W (106-110% FTP) / 3 min recovery @ 150W between = 25 min | 5 min @ 155W easy flush | 15 min cooldown @ 140-150W",
        "steps": [
            warmup(1, 900),
            repeat_group(2, 5, [
                interval(1, 120, 5, power=True),
                recovery(2, 180, 1),
            ]),
            recovery(3, 300, 1),
            cooldown(4, 900),
        ],
    },
}


def detect_zone(text):
    text_lower = (text or "").lower()
    m_z = re.search(r'\bz([1-7])\b', text_lower)
    if m_z:
        return int(m_z.group(1))
    if 'vo2' in text_lower:
        return 5
    if 'threshold' in text_lower or 'ftp' in text_lower:
        return 4
    if 'tempo' in text_lower or 'sweetspot' in text_lower:
        return 3
    if 'endurance' in text_lower or 'z2' in text_lower:
        return 2
    if 'recovery' in text_lower or 'easy' in text_lower or 'flush' in text_lower:
        return 1
    m_w = re.search(r'(\d+)\s*w\b', text_lower)
    if m_w:
        w = int(m_w.group(1))
        if w >= 320: return 6
        if w >= 285: return 5
        if w >= 250: return 4
        if w >= 200: return 3
        if w >= 155: return 2
        return 1
    return None


def parse_workout_steps(name, notes, total_duration_min=60):
    steps = []
    order = 1
    parts = [p.strip() for p in (notes or '').split('|') if p.strip()]

    for p in parts:
        p_lower = p.lower()
        # Skip pure coaching commentary sentences
        if any(marker in p_lower for marker in [
            'days to race', 'circumvictory is now', 'a race:',
            'monday recovery', 'friday buffer', 'complete rest',
            'final long aerobic', 'leave nothing out there', 'open the engine'
        ]):
            continue

        # Warmup check
        if 'warmup' in p_lower or (order == 1 and 'spin' in p_lower and 'cooldown' not in p_lower):
            m_time = re.search(r'(\d+)\s*min', p_lower)
            secs = int(m_time.group(1)) * 60 if m_time else 900
            steps.append(warmup(order, secs))
            order += 1
            continue

        # Cooldown check
        if 'cooldown' in p_lower:
            m_time = re.search(r'(\d+)\s*min', p_lower)
            secs = int(m_time.group(1)) * 60 if m_time else 600
            steps.append(cooldown(order, secs))
            order += 1
            continue

        # Repeat group check: e.g. 5x2 min @ 295W / 3 min recovery
        m_rep = re.search(r'(\d+)\s*[xX]\s*(\d+)\s*(min|m|s|sec)\b(.*?)/\s*(\d+)\s*(min|m|s|sec)\b(.*)', p)
        if m_rep:
            reps = int(m_rep.group(1))
            work_val = int(m_rep.group(2))
            work_unit = m_rep.group(3)
            work_secs = work_val * 60 if 'm' in work_unit else work_val
            work_desc = m_rep.group(4)
            work_zone = detect_zone(work_desc) or 5

            rest_val = int(m_rep.group(5))
            rest_unit = m_rep.group(6)
            rest_secs = rest_val * 60 if 'm' in rest_unit else rest_val

            sub_steps = [
                interval(1, work_secs, zone=work_zone, power=True),
                recovery(2, rest_secs, zone=1)
            ]
            steps.append(repeat_group(order, reps, sub_steps))
            order += 1
            continue

        # Steady block / Flush check
        m_time = re.search(r'(\d+)\s*min\b', p_lower)
        if m_time:
            secs = int(m_time.group(1)) * 60
            z = detect_zone(p)
            if 'flush' in p_lower or 'recovery' in p_lower or (z == 1):
                steps.append(recovery(order, secs, zone=1))
            else:
                steps.append(interval(order, secs, zone=z, power=(z is not None and z >= 3)))
            order += 1
            continue

    # Fallback if no steps generated
    if not steps:
        total_secs = (total_duration_min or 60) * 60
        wu_secs = min(900, int(total_secs * 0.2))
        cd_secs = min(600, int(total_secs * 0.15))
        main_secs = max(300, total_secs - wu_secs - cd_secs)
        steps = [
            warmup(1, wu_secs),
            interval(2, main_secs, zone=detect_zone(name) or 2, power=False),
            cooldown(3, cd_secs)
        ]
    return steps


def build_payload(name, desc, steps):
    return {
        "workoutName": name,
        "description": desc,
        "sportType": SPORT,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": SPORT,
            "workoutSteps": steps,
        }],
    }


def ensure_authenticated():
    if not os.path.exists(TOKEN_DIR):
        raise RuntimeError("Garmin token not found. Run garmin_auth.py to authenticate.")
    garth.resume(TOKEN_DIR)


def format_workout_title(target_date_str, title):
    try:
        dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        clean_title = (title or "Workout").strip()
        return f"{dt.strftime('%A, %b')} {dt.day}: {clean_title}"
    except Exception:
        return title or "Workout"


def cleanup_garmin_workouts():
    """Delete all previous workouts from Garmin Connect so only the current one displays."""
    try:
        existing_workouts = garth.connectapi("/workout-service/workouts?start=0&limit=50")
        for ew in existing_workouts:
            ew_id = ew.get("workoutId")
            if ew_id:
                try:
                    garth.connectapi(f"/workout-service/workout/{ew_id}", method="DELETE")
                except Exception as del_e:
                    print(f"Warning deleting previous Garmin workout {ew_id}: {del_e}")
    except Exception as list_e:
        print(f"Warning fetching workouts for cleanup: {list_e}")


def push_workout(workout_name, target_date, notes=None, duration_minutes=None):
    """
    Pushes a workout to Garmin Connect and schedules it for target_date.
    Cleans up previous workouts so only the current workout displays.
    Names workout formatted as 'Weekday, MonthAbrev Day: Title'.
    """
    try:
        ensure_authenticated()

        # Format name as 'Weekday, MonthAbrev Day: Title'
        formatted_name = format_workout_title(target_date, workout_name)

        # 1. Clean up previous workouts so only current displays
        cleanup_garmin_workouts()

        matched_key = None
        for k in WORKOUTS:
            if k.lower() == (workout_name or '').lower() or k.lower() in (workout_name or '').lower():
                matched_key = k
                break

        if matched_key:
            w = WORKOUTS[matched_key]
            desc = w["desc"]
            steps = w["steps"]
        else:
            desc = (notes or workout_name or formatted_name)[:250]
            steps = parse_workout_steps(workout_name, notes, duration_minutes)

        payload = build_payload(formatted_name, desc, steps)
        result = garth.connectapi("/workout-service/workout", method="POST", json=payload)
        workout_id = result["workoutId"]

        garth.connectapi(
            f"/workout-service/schedule/{workout_id}",
            method="POST",
            json={"date": target_date},
        )
        url = f"https://connect.garmin.com/modern/workout/{workout_id}"
        return {
            "ok": True,
            "workoutId": workout_id,
            "url": url,
            "name": formatted_name,
            "date": target_date,
        }
    except Exception as e:
        err_msg = str(e)
        resp = getattr(e, "response", None)
        if resp is not None and hasattr(resp, "text"):
            err_msg = f"{err_msg}: {resp.text[:300]}"
        return {
            "ok": False,
            "error": err_msg,
            "name": workout_name,
            "date": target_date,
        }


def push(workout_key, target_date):
    res = push_workout(workout_key, target_date)
    if res["ok"]:
        print(f"✓ Pushed '{res['name']}' to Garmin Connect: {res['workoutId']}")
        print(f"✓ Scheduled to {target_date}")
        print(f"  {res['url']}")
        return res["workoutId"]
    else:
        print(f"✗ Failed to push workout: {res.get('error')}")
        sys.exit(1)


def inspect_existing():
    ensure_authenticated()
    workouts = garth.connectapi("/workout-service/workouts?start=0&limit=1")
    if not workouts:
        print("No existing workouts found.")
        return
    detail = garth.connectapi(f"/workout-service/workout/{workouts[0]['workoutId']}")
    print(json.dumps(detail, indent=2))


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect_existing()
        sys.exit(0)

    flags        = [a for a in sys.argv[1:] if a.startswith("--")]
    positional   = [a for a in sys.argv[1:] if not a.startswith("--")]
    date_args    = [a for a in positional if len(a) == 10 and a[4] == '-' and a[7] == '-']
    key_args     = [a for a in positional if a not in date_args]
    target_date  = date_args[0] if date_args else date.today().isoformat()
    workout_key  = key_args[0] if key_args else "Sharpener 6x45s Z5"
    push(workout_key, target_date)

