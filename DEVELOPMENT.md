# Development notes

Architecture and design-decision notes for anyone working on this codebase. For a feature overview, see [README.md](README.md).

## Database schema

| Table | Purpose |
|---|---|
| `workouts` | Planned future workouts (editable targets): `date` (PK), `name`, `tss`, `duration_minutes`, `notes` |
| `strava_activities` | Raw Strava API data. Single source of truth for *completed* workouts, populated only by `strava_import.py`, never edited directly. |
| `activity_streams` | Per-activity HR-zone time breakdown and lap/quality annotations, backfilled separately from the main sync (`strava_import.py --streams` / `--laps`). |
| `settings` | Key/value store: `ftp`, `lthr`, `ctlDecay`, `atlDecay`, `startCTL`, `startATL`, `startDate`, API keys. |
| `tss_overrides` | Manual TSS correction for a given date, takes priority over the computed value without touching the underlying Strava row. |
| `phases` / `workout_menu` | Training phase date ranges and a reusable library of interval structures, used by the AI coach's context. |
| `coach_memory` | The AI coach's persistent memory: `category` (`rule` / `goal` / `observation`), `content`, `active` flag. Seeded with a small set of defaults on first run, extended by the coach itself (subject to human approval) over time. |
| `coach_messages` | Chat history with the AI coach, including any `proposed_json` workout edits and whether they were applied. |

There is deliberately no `completed_workouts` table. An earlier version had one, but it was a lossy derived copy of `strava_activities` that could drift out of sync. Completed-workout data is always computed live from `strava_activities` in `load_all()`.

## Data flow

- `load_all()` in `server.py` aggregates `strava_activities` into a per-day completed-workout dict on every `/api/data` request. This is the only place completed data is read from.
- `POST /api/sync-strava` runs `strava_import.py --forward` as a subprocess and reports how many new activities were added.
- `POST /api/import` replaces `workouts` + `settings` only. It never touches `strava_activities`, so re-importing a plan can't corrupt ride history.
- A JSON export (`training-plan-import.json`) only ever contains `{planned, settings}` for the same reason.
- `training_snapshot.json` is written after every sync (manual or scheduled) as a lightweight, read-only summary of the last 90 days: settings, phases, races, and per-activity detail including HR-zone breakdowns. It exists so that a downstream consumer (e.g. an AI assistant reading the repo) can get current state without needing direct DB access or query logic of its own.

## TSS calculation

`calc_activity_tss()` is implemented in both `server.py` (for the live PMC aggregation) and `coach_engine.py` (for coach context), kept in sync manually since they're small and rarely change. Priority order:

1. **Cycling**: normalized power (`weighted_average_watts`) from a power meter if available, else average watts from a power meter, else `suffer_score` as a fallback. (Strava's kJ-based estimate is intentionally *not* used for non-power-meter rides; it under-represents effort badly enough to be worse than the fallback.)
2. **Running**: HR-based TSS (`hrTSS`) using average HR relative to LTHR, else `suffer_score`.
3. **Everything else**: `suffer_score` as a generic proxy.

### Calibration

Checked against a sample of real matched activities (Strava's own suffer-score/TSS vs. this model's output):

| Workout type | Accuracy |
|---|---|
| Sweet Spot | Excellent |
| VO2max | Excellent |
| Threshold | Good |
| Tempo | Good |
| Long outdoor rides | Underestimated: outdoor long rides run longer and hotter than their indoor/trainer equivalents |
| Zwift races | Underestimated: races run harder than a similarly-labeled structured workout |
| Recovery rides | Noisiest category: Strava's auto-matching to the wrong day/activity is the most common cause, not the TSS formula itself |

This is the kind of error pattern you'd expect from a duration-times-intensity model that has no way to know a ride was unusually hard relative to its average power. It's a known, accepted limitation rather than a bug.

## AI coach design

`coach_engine.py` separates **context assembly** (deterministic, pure Python, no LLM involved) from **plan proposal** (the LLM call). The context payload includes:

- Current settings (FTP, LTHR, load-model decay constants)
- 14-day detailed training history with per-activity TSS, intensity factor, and HR
- The upcoming 35-day planned calendar
- Active training phases and target races
- The coach's own persistent memory (active rules/goals/observations only)

Both the Gemini and Claude code paths send this same context and require the model to respond via a single structured tool/function call (`propose_plan_updates`) rather than free text. The schema requires `date`, `action` (`update`/`create`/`delete`), `name`, `tss`, `duration_minutes`, and a reason for every proposed change. Proposals are validated (e.g. dates must be real, required fields present) before being shown to the user, and nothing is written to `workouts` or `coach_memory` until the user explicitly applies the proposal via `/api/coach/apply`. This human-in-the-loop step is intentional: an LLM should be able to *suggest* a training plan change, but never silently mutate the plan.

Gemini additionally uses a model cascade: if the primary model returns a transient error (429/500/503, or an "overloaded" message), the request automatically retries against the next model in the list before giving up.

## UI notes (`training-plan.html`)

- **Past tab**: window ends at *today* (not yesterday), and days are reversed so the most recent appears first. Shows completed Strava data, read-only.
- **Upcoming tab**: filters out any day that already has a completed entry, so a ride that's already happened doesn't also show up as a future plan item.
- Completed rows are clickable and deep-link to the matching Strava activity (`strava_id` is the ID of whichever activity had the highest TSS that day, since some days have multiple activities).

## Workout description convention

Every workout's `notes` field is expected to account for 100% of its `duration_minutes`: warmup plus all intervals plus all recoveries plus cooldown should sum to the total. This isn't enforced in code, but both the planner UI and the AI coach's prompt follow the convention:

```
[warmup] | [main set: reps x duration @ target / recovery @ target] | [cooldown] | [context notes]
```

Example (60 min VO2max session): `15min warmup ramp 135-205W | 5x5min@285W / 3min@155W between sets | 8min cooldown` = 15 + (5x5 + 4x3) + 8 = 60 minutes. The Garmin and Zwift workout generators both parse this structure to build their respective step/interval formats.
