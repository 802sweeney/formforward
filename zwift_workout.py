#!/usr/bin/env python3
"""
Generate and manage Zwift custom workouts (.zwo XML files).
Saves to ~/Documents/Zwift/Workouts/<athlete_id>/
Cleans up previous workouts into ~/Documents/Zwift/Workouts_Archive/
"""
import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.dom import minidom

ZWIFT_BASE = os.path.expanduser("~/Documents/Zwift/Workouts")
ARCHIVE_BASE = os.path.expanduser("~/Documents/Zwift/Workouts_Archive")


def get_athlete_dir():
    if not os.path.exists(ZWIFT_BASE):
        os.makedirs(ZWIFT_BASE, exist_ok=True)
    try:
        entries = os.listdir(ZWIFT_BASE)
        numeric_dirs = [e for e in entries if e.isdigit() and os.path.isdir(os.path.join(ZWIFT_BASE, e))]
        if numeric_dirs:
            return os.path.join(ZWIFT_BASE, numeric_dirs[0])
    except Exception:
        pass
    # Default to 172152
    d = os.path.join(ZWIFT_BASE, "172152")
    os.makedirs(d, exist_ok=True)
    return d


def format_workout_title(target_date_str, title):
    try:
        dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        clean_title = (title or "Workout").strip()
        return f"{dt.strftime('%A, %b')} {dt.day}: {clean_title}"
    except Exception:
        return title or "Workout"


def cleanup_zwift_workouts(athlete_dir):
    """
    Moves all existing .zwo files in the athlete's workout folder to Workouts_Archive
    so that only the current workout displays in Zwift.
    """
    os.makedirs(ARCHIVE_BASE, exist_ok=True)
    moved = []
    if os.path.exists(athlete_dir):
        for fname in os.listdir(athlete_dir):
            if fname.endswith(".zwo"):
                src = os.path.join(athlete_dir, fname)
                dest = os.path.join(ARCHIVE_BASE, fname)
                try:
                    shutil.move(src, dest)
                    moved.append(fname)
                except Exception as e:
                    print(f"Warning: could not move {fname} to archive: {e}")
    return moved


def extract_power_ratio(text, default=0.65, ftp=278):
    if not text:
        return default
    text_lower = text.lower()

    # 1. Match % FTP, e.g. 106-110% FTP or 95% FTP
    m_pct = re.search(r'(\d+)(?:\s*-\s*(\d+))?%\s*ftp', text_lower)
    if m_pct:
        p1 = float(m_pct.group(1))
        p2 = float(m_pct.group(2)) if m_pct.group(2) else p1
        return round(((p1 + p2) / 2.0) / 100.0, 2)

    # 2. Match watts range, e.g. 295-305W or 300W
    m_w = re.search(r'(\d+)(?:\s*-\s*(\d+))?\s*w\b', text_lower)
    if m_w:
        w1 = float(m_w.group(1))
        w2 = float(m_w.group(2)) if m_w.group(2) else w1
        avg_w = (w1 + w2) / 2.0
        if ftp > 0:
            return round(avg_w / ftp, 2)

    # 3. Match zone
    m_z = re.search(r'\bz([1-7])\b', text_lower)
    if m_z:
        z = int(m_z.group(1))
        z_map = {1: 0.50, 2: 0.65, 3: 0.82, 4: 0.96, 5: 1.10, 6: 1.25, 7: 1.50}
        return z_map.get(z, default)

    if 'vo2' in text_lower: return 1.10
    if 'threshold' in text_lower: return 0.96
    if 'tempo' in text_lower or 'sweetspot' in text_lower: return 0.82
    if 'endurance' in text_lower: return 0.65
    if 'recovery' in text_lower or 'easy' in text_lower or 'flush' in text_lower: return 0.50
    return default


def build_zwo_xml(formatted_name, description, notes, duration_min=60, ftp=278):
    root = ET.Element('workout_file')
    ET.SubElement(root, 'author').text = 'Form Forward Virtual Coach'
    ET.SubElement(root, 'name').text = formatted_name
    ET.SubElement(root, 'description').text = description or notes or formatted_name
    ET.SubElement(root, 'sportType').text = 'bike'
    tags = ET.SubElement(root, 'tags')
    ET.SubElement(tags, 'tag', {'name': 'FormForward'})

    workout = ET.SubElement(root, 'workout')

    parts = [p.strip() for p in (notes or '').split('|') if p.strip()]
    has_elements = False

    for p in parts:
        p_lower = p.lower()
        if any(marker in p_lower for marker in [
            'days to race', 'circumvictory is now', 'a race:',
            'monday recovery', 'friday buffer', 'complete rest',
            'final long aerobic', 'leave nothing out there', 'open the engine'
        ]):
            continue

        # Warmup
        if 'warmup' in p_lower or ('spin' in p_lower and not has_elements and 'cooldown' not in p_lower):
            m_time = re.search(r'(\d+)\s*min', p_lower)
            secs = int(m_time.group(1)) * 60 if m_time else 900
            ET.SubElement(workout, 'Warmup', {
                'Duration': str(secs),
                'PowerLow': '0.45',
                'PowerHigh': '0.75',
                'pace': '0'
            })
            has_elements = True
            continue

        # Cooldown
        if 'cooldown' in p_lower:
            m_time = re.search(r'(\d+)\s*min', p_lower)
            secs = int(m_time.group(1)) * 60 if m_time else 600
            ET.SubElement(workout, 'Cooldown', {
                'Duration': str(secs),
                'PowerLow': '0.55',
                'PowerHigh': '0.40',
                'pace': '0'
            })
            has_elements = True
            continue

        # Repeat group check: e.g. 5x2 min @ 295W / 3 min recovery
        m_rep = re.search(r'(\d+)\s*[xX]\s*(\d+)\s*(min|m|s|sec)\b(.*?)/\s*(\d+)\s*(min|m|s|sec)\b(.*)', p)
        if m_rep:
            reps = int(m_rep.group(1))
            work_val = int(m_rep.group(2))
            work_unit = m_rep.group(3)
            work_secs = work_val * 60 if 'm' in work_unit else work_val
            work_desc = m_rep.group(4)
            work_ratio = extract_power_ratio(work_desc, default=1.10, ftp=ftp)

            rest_val = int(m_rep.group(5))
            rest_unit = m_rep.group(6)
            rest_secs = rest_val * 60 if 'm' in rest_unit else rest_val
            rest_desc = m_rep.group(7)
            rest_ratio = extract_power_ratio(rest_desc, default=0.50, ftp=ftp)

            ET.SubElement(workout, 'IntervalsT', {
                'Repeat': str(reps),
                'OnDuration': str(work_secs),
                'OffDuration': str(rest_secs),
                'OnPower': f'{work_ratio:.2f}',
                'OffPower': f'{rest_ratio:.2f}',
                'pace': '0'
            })
            has_elements = True
            continue

        # Steady / Flush check
        m_time = re.search(r'(\d+)\s*min\b', p_lower)
        if m_time:
            secs = int(m_time.group(1)) * 60
            if 'flush' in p_lower or 'recovery' in p_lower:
                ratio = extract_power_ratio(p, default=0.50, ftp=ftp)
            else:
                ratio = extract_power_ratio(p, default=0.65, ftp=ftp)
            ET.SubElement(workout, 'SteadyState', {
                'Duration': str(secs),
                'Power': f'{ratio:.2f}',
                'pace': '0'
            })
            has_elements = True
            continue

    # Fallback
    if not has_elements:
        total_secs = (duration_min or 60) * 60
        wu_secs = min(900, int(total_secs * 0.2))
        cd_secs = min(600, int(total_secs * 0.15))
        main_secs = max(300, total_secs - wu_secs - cd_secs)
        main_ratio = extract_power_ratio(formatted_name, default=0.65, ftp=ftp)
        ET.SubElement(workout, 'Warmup', {
            'Duration': str(wu_secs),
            'PowerLow': '0.45',
            'PowerHigh': '0.75',
            'pace': '0'
        })
        ET.SubElement(workout, 'SteadyState', {
            'Duration': str(main_secs),
            'Power': f'{main_ratio:.2f}',
            'pace': '0'
        })
        ET.SubElement(workout, 'Cooldown', {
            'Duration': str(cd_secs),
            'PowerLow': '0.55',
            'PowerHigh': '0.40',
            'pace': '0'
        })

    rough = ET.tostring(root, 'utf-8')
    return minidom.parseString(rough).toprettyxml(indent='    ')


def push_zwift_workout(workout_name, target_date, notes=None, duration_minutes=None, ftp=278):
    try:
        formatted_name = format_workout_title(target_date, workout_name)
        athlete_dir = get_athlete_dir()

        # 1. Clean up previous workouts so only this one displays in Zwift
        cleaned = cleanup_zwift_workouts(athlete_dir)

        # 2. Generate XML
        desc = (notes or workout_name or '')[:300]
        xml_content = build_zwo_xml(formatted_name, desc, notes, duration_min=duration_minutes, ftp=ftp)

        # 3. Create safe filename: e.g. Thursday_Oct_15_Race_Sharpeners.zwo
        base_name = re.sub(r'[^A-Za-z0-9]+', '_', formatted_name).strip('_')
        filename = f"{base_name}.zwo"
        dest_path = os.path.join(athlete_dir, filename)

        with open(dest_path, 'w', encoding='utf-8') as f:
            f.write(xml_content)

        return {
            "ok": True,
            "name": formatted_name,
            "filename": filename,
            "path": dest_path,
            "cleaned_count": len(cleaned),
            "date": target_date
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "name": workout_name,
            "date": target_date
        }