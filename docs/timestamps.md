# Timestamps, timezones, and folder naming

How frameforge handles time. Locked design as of 2026-06-04.

---

## TL;DR

| Domain | Where used | Reference |
| --- | --- | --- |
| Wall-clock local time (TZ-aware) | Folder names, log line timestamps, chunk_index math | Host TZ via `/etc/localtime` |
| Monotonic time | Rate-limiter cooldowns, sleep budgets, retry windows | Opaque; immune to clock changes |
| Hardware timestamp (Basler) | Not currently captured (revisit if needed for drift metric or pre-open routing) | Camera nanoseconds |
| Filesystem mtime | OS-managed, consumed downstream | UTC epoch stored; displayed in local |

Locked behaviors:
1. `session_name` snapshotted once at supervisor boot in local TZ; stable across the entire run (could be days, weeks, years).
2. `recording_start_str` derived per chunk-open as **midnight of the current local day** (`YYYY-MM-DD-00-00-00`). Rotates daily.
3. `chunk_index` = **TZ-aware elapsed hours since today's midnight (local)**. Bounded 0–22 (DST spring), 0–23 (normal), 0–24 (DST fall).
4. Chunk closes when `frames_written == 180000` (primary trigger) OR `_current_chunk_index() != opened_index` (sanity backstop).

---

## The three time sources

### Wall-clock local time (TZ-aware)

- Source: `datetime.datetime.now().astimezone()` — TZ-aware, host's local TZ.
- Used for: every operator-facing artifact (folder names, log timestamps, chunk_index, session_name).
- **TZ-aware subtraction is essential**: two TZ-aware datetimes anchor to UTC internally, so subtracting them gives real elapsed seconds. Naive datetimes do calendar-field arithmetic and don't see DST. The DST gap / overlap is invisible to naive code.

### Monotonic time

- Source: `time.monotonic()` — opaque ever-increasing seconds; no epoch; immune to NTP / DST / manual clock changes.
- Used for: rate-limiter cooldowns, sleep budgets, retry windows. Throughout the codebase already.
- **Not** used for chunk boundaries (TZ-aware wall-clock owns that).

### Hardware timestamp (Basler)

- Currently not captured (removed when synthetic-frame design was dropped).
- Revisit if/when `acq.<cam>.timestamp_drift_ms` is implemented or pre-open chunks (Topic 6) needs frame-ordering by camera clock. Parking-lot tracked in `action_items.md`.

---

## Folder + filename contract

```
{scratch_root}/
  {session_name}/                  e.g. 2026-06-04-Frameforge        (boot snapshot, stable)
    {camera_id}/                   e.g. cam_08
      {recording_start_str}/       e.g. 2026-06-04-00-00-00          (midnight of current day, local)
        {camera_id}.{HH}.mp4       e.g. cam_08.14.mp4                (HH = elapsed hours from folder's midnight)
```

- **`session_name`**: snapshotted at boot via `local_now().strftime("%Y-%m-%d") + "-Frameforge"`. Stable for the entire run. Lives in `Context`.
- **`recording_start_str`**: derived per chunk-open as midnight of the current local day. Rotates at midnight. Not stored in Context.
- **`HH`**: TZ-aware elapsed hours since the folder's midnight, formatted `%02d`. On normal days this equals the wall-clock hour (0-23). On DST days it's sequential without gaps or collisions (see below).

### HH math (canonical)

```python
midnight = datetime.datetime.now().astimezone().replace(
    hour=0, minute=0, second=0, microsecond=0)
now = datetime.datetime.now().astimezone()
chunk_index = int((now - midnight).total_seconds() // 3600)
```

Because both `midnight` and `now` are TZ-aware, the subtraction gives real elapsed seconds — not wall-clock seconds. DST handling is automatic.

### Example timelines

**Normal day** — supervisor boots at 14:30 on 2026-06-04:

| Day | Folder | Files | HH range |
| --- | --- | --- | --- |
| 2026-06-04 (boot) | `.../cam_08/2026-06-04-00-00-00/` | `cam_08.14.mp4` … `cam_08.23.mp4` | 14–23 |
| 2026-06-05 | `.../cam_08/2026-06-05-00-00-00/` | `cam_08.00.mp4` … `cam_08.23.mp4` | 0–23 |

**DST spring forward** (PST → PDT, lose 02:00 hour):

| Wall clock | Elapsed (real) hours | HH (chunk_index) |
| --- | --- | --- |
| 00:00 → 01:00 | 0 → 1 | 0 |
| 01:00 → 03:00 (DST jump) | 1 → 2 | 1 |
| 03:00 → 04:00 | 2 → 3 | 2 |
| … | … | … |
| 23:00 → 24:00 | 22 → 23 | 22 |

Day produces **23 chunks (0–22)**, sequential, no gap. `cam_08.02.mp4` covers wall-clock 03:00–04:00; `cam_08.01.mp4` spans the DST jump.

**DST fall back** (PDT → PST, repeat 01:00 hour):

| Wall clock | Elapsed (real) hours | HH (chunk_index) |
| --- | --- | --- |
| 00:00 → 01:00 | 0 → 1 | 0 |
| 01:00 → 02:00 (first pass) | 1 → 2 | 1 |
| 02:00 → 01:00 (DST jump) | 2 → 3 | 2 |
| 01:00 → 02:00 (second pass) | 3 → 4 | 3 |
| 02:00 → 03:00 | 4 → 5 | 4 |
| … | … | … |
| 23:00 → 24:00 | 24 → 25 | 24 |

Day produces **25 chunks (0–24)**, sequential, no collision. The two passes through 01:00 get separate files (`cam_08.01.mp4` first pass, `cam_08.03.mp4` second pass).

---

## Chunk lifecycle

### Open

Each iteration of the encoder's outer loop:
1. Compute `now = local_now().astimezone()`.
2. `chunk_index = (now - midnight).total_seconds() // 3600`.
3. `recording_start_str = midnight.strftime(...)`.
4. Compute `chunk_path = scratch/session/cam/recording_start_str/cam.HH.mp4`.
5. If `chunk_path` already exists (crash + restart mid-chunk, dedup guard), enter **idle mode** until chunk_index changes.
6. Else open `.part` and start recording.

### Close

Inside `_record_chunk`, the loop exits when ANY of:
- `frames_written >= 180_000` (primary trigger; 3600 s × 50 fps).
- `_current_chunk_index() != opened_index` (sanity backstop; closes when elapsed-hour boundary crosses, DST-safe).
- `drain` is set (graceful shutdown — current semantic, may be revisited).
- `WriterDied` raised (encoder pipeline failure; partial finalized).

After close: `.part` → `.mp4` atomic rename.

### Idle (same-hour restart / crash recovery)

`_idle_until_next_chunk`:
- One INFO log on entry, one on resume
- Drains data_queue and releases ring slots (acq isn't artificially back-pressured)
- Honors drain immediately
- Sets `enc.<cam>.idle{cam=...}` gauge to 1 during idle, 0 on resume — dashboard visibility

Replaces the previous "discard mode" mechanism. No per-frame counter.

---

## Filesystem mtime + log timestamps

**Filesystem mtime**: stored as UTC epoch seconds by the kernel. `ls -l` and downstream consumers convert to local on display. In PDT (UTC-7) or PST (UTC-8), this shows the +7/+8 offset naturally. No special handling in frameforge.

**Log timestamps**: local time, `YYYY-MM-DD HH:MM:SS` (space-separator, no TZ suffix, no brackets). Matches `journalctl` default formatting. Set in `frameforge/logging_setup.py`.

Canonical log line:
```
2026-06-04 14:08:22 INFO  [enc:cam_08] opened chunk cam=cam_08 index=14 target=180000 path=/scratch/.../cam_08.14.mp4.part
```

The `[enc:cam_08]` tag comes from `multiprocessing.Process.name` (set by supervisor when spawning each worker), surfaced via `%(processName)s` in the formatter.

---

## What changed in code (2026-06-04 revision)

- `Encoder._current_chunk_index()` is TZ-aware elapsed-hours-since-midnight, not `datetime.now().hour`.
- `Encoder._record_chunk` close condition uses `_current_chunk_index() != opened_index` (DST-safe), not `now.hour != opened_hour`.
- `Encoder._discard_until_next_chunk` renamed to `_idle_until_next_chunk`. Removes per-frame `enc.<cam>.discarded` counter; adds `enc.<cam>.idle` 0/1 gauge and INFO entry/exit logs.
- Log lines: `idx=` → `index=` everywhere; `FINALIZED` → `finalized`; arrows removed from messages where the source/target is now `key=val`.
- `enc.<cam>.discarded` metric retired (replaced by `enc.<cam>.idle` gauge).

No new helper module. The TZ-aware derivations are inlined in encoder.py at the two callsites that need them.
