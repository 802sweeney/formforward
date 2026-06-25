# Agent Notes — Training Plan App

Project: local web app (training-plan.html + server.py + training.db)
Stack: single-file HTML/JS frontend, Python stdlib HTTP server, SQLite

---

## Constraints

### SQLite lock
The server holds an exclusive lock on `training.db` through the FUSE-mounted Google Drive.
Any direct write from the sandbox fails with `sqlite3.OperationalError: disk I/O error`.
**Fix: ask the user to stop the server before doing direct DB writes. Don't do the import-JSON workaround unless the user explicitly can't stop it.**

### FUSE mount (Google Drive)
- Reads are fine
- Writes usually work but `conn.close()` often throws `disk I/O error` even after a successful commit — the data IS written; the error is noise from FUSE on close
- Binary files (xlsx) cannot be read over the FUSE mount at all — ask for a CSV export instead
- DB path: `/sessions/busy-sharp-bell/mnt/My Drive/bikes/training/training.db`
- Server script: `/sessions/busy-sharp-bell/mnt/My Drive/bikes/training/server.py`
- HTML: `/sessions/busy-sharp-bell/mnt/My Drive/bikes/training/training-plan.html`

### Sandbox ↔ server
The server runs on `127.0.0.1:5678` on the user's machine. The sandbox cannot reach it.
Don't try to curl/POST to it.

---

## Architecture

### Tables
| Table | Purpose |
|---|---|
| `workouts` | Planned future workouts (editable targets). date PK, name, tss, duration_minutes, notes |
| `strava_activities` | Raw Strava API data. Single source of truth for completed workouts. Populated only by `strava_import.py` |
| `settings` | Key/value: ftp, lthr, ctlDecay, atlDecay, startCTL, startATL, startDate |
| `strava_sync` | Cursor state for strava_import.py backward/forward sync |

`completed_workouts` was eliminated — it was a redundant lossy view of `strava_activities`. Do not recreate it.

### Data flow
- `load_all()` in server.py aggregates `strava_activities` → per-day completed dict on every `/api/data` request
- Strava sync: `POST /api/sync-strava` runs `strava_import.py --forward` as subprocess
- Import: `POST /api/import` replaces `workouts` + `settings` only, never touches `strava_activities`
- Export JSON: only exports `{planned, settings}` — completed data always comes from Strava

### TSS calculation
`calc_activity_tss()` lives in both `server.py` (for load_all aggregation) and `strava_import.py` (for reference).
Priority: device_watts NP → average_watts → kJ-estimated watts → suffer_score fallback.
FTP default: 250W (read from settings).

---

## Known accuracy of TSS estimates by workout type

From calibration against actual Strava data (77 matched workouts):

| Type | IF used | Implied IF | Accuracy |
|---|---|---|---|
| Sweet Spot | 0.83 | 0.82 | ✅ excellent |
| VO2max | 0.87 | 0.88 | ✅ excellent |
| Threshold | 0.84 | 0.86 | ✅ good |
| Tempo | 0.78 | 0.76 | ✅ good |
| Long Ride | 0.73 | 0.96 | ❌ badly underestimated — outdoor rides are 3-4h, not 75min |
| Zwift Race | 0.88 | 0.96 | ❌ underestimated — races run hotter |
| Mixed Intensity | 0.82 | 0.93 | ❌ underestimated when it's actually a race |
| Recovery | 0.59 | 0.72 | ⚠️ noisy — often Strava date-matches wrong activity |

**Long Ride TSS fix**: user's recent outdoor long rides produce 195–205 TSS (3.5–4 hours).
Use ~180 TSS for April/May long ride Sundays, not the plan's 67 TSS.

---

## UI / HTML notes

### Tab logic (buildWeekRows)
- **Past tab** (`isPastTab=true`): windowEnd = `today` (not yesterday — fixed bug). Days reversed within each week (most recent day first). Shows completed Strava data, read-only.
- **Upcoming tab** (`isPastTab=false`): filters out days where `state.completed[d]` exists — completed rides do not appear here.
- `daysToShow` is the filtered/reversed array; `daysInWindow` is used only for weekly TSS sum.
- If all days in a week are completed, the week header is also suppressed (`if (!daysToShow.length) return`).

### Strava links
Completed workout rows have `onclick → window.open('https://www.strava.com/activities/{strava_id}', '_blank')`.
`strava_id` = ID of highest-TSS activity for that day, populated during `load_all()` aggregation.

---

## Workout description rules

**Every workout description must account for 100% of the total duration.** Warmup + all intervals + all recoveries + cooldown must add up to `duration_minutes`. If they don't, adjust the warmup or cooldown.

**Format:** `[warmup details] | [main set with reps × duration @ watts / recovery @ watts] | [cooldown details] | [context/notes]`

**Example for 60min VO2max:**
`15min warmup ramp 135→205W (2×20s@290W pick-ups at min 12+14) | 5×5min@285W / 3min@155W between sets | 8min cooldown 4min@175W + 4min@140W`
= 15 + (5×5 + 4×3) + 8 = 15 + 37 + 8 = 60 ✓

**Tuesday/Thursday workouts must be genuinely hard (TSS ~80–85 for 60min).** The user has limited weekday time and needs those sessions to count. Target NP ~220–225W (IF ~0.88–0.90) across the full session. Check against actual Strava data: Mar 17 "6x4 VO2 Max" = 56min TSS 77, Mar 19 "FTP 2x13" = 60min TSS 77, Mar 31 "Race Sim" = 60min TSS 79. Plan TSS of 67 on Tue/Thu was too low.

**Openers/pre-race rides are legitimately mostly easy.** It's fine that 35–40min of a 60min opener is warmup/cooldown — that's the point. Just be explicit about it so the user knows.

**Don't invent new workout types on rest days (Mon/Wed).** User keeps those as rest days.

---

## Workflow mistakes to avoid

1. **Don't pad workout visualizations to hit a total time.** Show only the structured content. If the plan says 41 min of work, graph 41 min — don't add 24 min of easy spin filler.

2. **Don't write to the DB without asking the user to stop the server first.** When server is stopped, copy DB to /tmp, edit there, then `cp` back — direct sqlite3 access over FUSE throws disk I/O errors but `cp` works fine.

3. **Don't use the import-JSON workaround as a first resort.** Just ask: "can you stop the server for a sec?"

4. **Don't query the DB for things that don't need it.** Workout graphs, visualizations, analysis — all can be built from the CSV or from values already in context.

5. **Don't add Zone 2 rides to rest days without asking.** The user explicitly keeps Mon/Wed as rest days.

6. **The past tab window ends at `today`, not `yesterday`.** A completed ride today should appear on the past tab, not the upcoming tab.

---

## Race context

- **Race day**: May 31, 2026 (MTB race)
- **Vacation**: May 6–13 (no training, unavoidable CTL drop ~13%)
- **Current CTL** (as of late March 2026): ~48–50
- **Target CTL at race**: ~43–48 with TSB +7 to +12 on race morning
- FTP: 250W (last tested Dec 31, 2025 — ~250W estimate)

---

## File locations

```
/sessions/busy-sharp-bell/mnt/My Drive/bikes/training/
  server.py               HTTP server + DB logic
  training-plan.html      Single-file frontend
  training.db             SQLite database
  strava_import.py        Strava API → strava_activities sync
  training-plan-import.json   Last generated import payload
  strava_api/.env         STRAVA_CLIENT_ID / SECRET / REFRESH_TOKEN

/sessions/busy-sharp-bell/mnt/uploads/
  2026 Training Plan.xlsx - Training Plan.csv   Full training plan CSV
```
