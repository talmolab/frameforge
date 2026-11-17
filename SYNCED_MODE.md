# Synced mode: hardware-triggered multi-camera sessions

Design notes, 2026-10-20. Status: proposed, nothing built. Modeled on how
[panopticon](https://github.com/talmolab/panopticon) (`gui_app/trigger_source.py`,
`backends/`, `grab_thread.py`, `frame_sync.py`) does hardware sync.

Terminology: "session" here means one bounded, hardware-triggered recording. It is
not the continuous mode's `session_name` folder (a recording epoch), and not an
experiment (a time window × cages in Postgres). Rename before building if the
collision bites.

## Why a second mode

Continuous mode free-runs every camera on its own clock and timestamps frames on
the host after `RetrieveResult`, so cross-camera alignment is ms-level and carries
scheduling jitter. Sub-frame simultaneity (3D pose) needs one TTL edge fanned out
to every camera and alignment on the camera's own trigger counter, not on host time.

## How panopticon does it

**Camera init is the same for every trigger source.** Recording always uses
`set_triggered`:

- `TriggerSelector=FrameStart` (set before `TriggerMode`), `TriggerMode=On`,
  `TriggerSource=Line1`, `TriggerActivation=RisingEdge`.
- `AcquisitionFrameRate` set to a ceiling above the trigger rate (165), never to
  fps. The limiter still enforces `exposure + 1/rate` while triggered; leaving it at
  100 caused panopticon's 50 fps bug (every second trigger skipped).

**Timestamps are the same for every trigger source.**

- `BlockID`: the trigger ordinal. `StartGrabbing` resets it to 1, so ID N is the
  same pulse on every camera. Alignment, kick-out and `blockids.npy` are built on it.
- `TimeStamp`: the camera's own clock in ns, which must keep running across a
  restart. Used for the stall resync and the block-ID rate check.
- The host clock is used only for stall and silence detection.

**Only the start/stop protocol differs** (`TriggerSource`):

| | `board` (Arduino/Teensy over serial) | `external` (pulse generator, DAQ) |
|---|---|---|
| Start | after the barrier: send `<n>,<pins>,<fps>`, wait for the `RDY` ack, one reset-and-retry | send nothing; watch the armed cameras for max(0.5 s, 5 periods) and refuse if any frame arrives; prompt the operator; first-trigger timeout |
| Stop | send the stop; grab loops end on retrieve timeout | prompt; all cameras silent for ~2× the silence threshold; force after 30 s |
| Rate check | the board owns the clock | block-ID rate check is the only check on fps |
| Stimulation | yes | no |

## What frameforge does today (continuous)

- `PylonSource._apply_defaults` sets `AcquisitionFrameRate = fps` and never
  touches `TriggerMode`: free-run.
- `Frame.ts_ns = time.time_ns()` after `RetrieveResult`: delivery time, not
  exposure time. `BlockID` is read but only feeds the missed-frames metric.
- Acquisition reopens and calls `StartGrabbing` again on disconnect. That is fine
  in free-run, but under sync it silently resets one camera's block IDs to 1.
- Encoders roll chunks independently on the wall clock; the `.h5` sidecar holds
  host timestamps only.

## The split

```
                          ┌──────────────── config: mode = continuous | synced ────────────────┐
            CONTINUOUS (today, unchanged)                         SYNCED (new)
            ─────────────────────────────                         ────────────
SESSION     Supervisor runs forever;                    SessionSupervisor (long-running daemon)
CONTROL     watchdog respawns workers                     idles in IDLE; each session runs
                                                          IDLE → ARM → BARRIER → RUN → STOP → FINALIZE → IDLE
                                                          no respawn of acq/encoder mid-session

TRIGGER     none (camera internal rate)                 frameforge/triggers/ (protocol + registry + lazy import)
                                                          ├─ board:    serial start(pins,fps) + RDY ack / stop
                                                          └─ external: no I/O; pre-barrier silence check,
                                                                       first-trigger timeout, end-silence

SOURCE      PylonSource.open()                          PylonSource(trigger=True)
INIT        AcquisitionFrameRate = fps                    FrameStart → TriggerMode=On, Line1, RisingEdge
                                                          AcquisitionFrameRate = ceiling (> fps)
                                                          GevTimestampTickFrequency must be 1 GHz
                                                          StartGrabbing exactly once per session

GRAB        Frame(array, host ts, seq)                  Frame(array, host ts, block_id, dev_ts)
            disconnect → reopen, continue                 refuse block_id ≤ 0, unwrap 16-bit
                                                          frames before barrier → abort start
                                                          disconnect → retire camera, never re-arm
                                                          retrieve timeout after stop → normal exit

  ── shared-memory ring + queue (same; payload gains block_id, dev_ts) ──────────────────────────

KICK-OUT    n/a                                         A (first): none at capture, offline block-ID intersect
                                                        B (later): cross-process coordinator before encoders

ENCODER     ChunkScheduler: midnight-aligned            SessionScheduler: one file per session
                                                          <root>/<session_id>/<cam>/<cam>.mp4
SIDECAR     .h5: host ts                                .h5: host ts + block_id + dev_ts, session metadata

BROADCAST   ───────────────────── same (subsampled tee off the grab loop) ─────────────────────────
TRANSFER    ───────────────────── same (uploads finished .mp4 + .h5) ─────────────────────────────
METRICS     ───── same, plus barrier_wait_s, frames_before_barrier, block_id_gaps, retired ────────
```

## Session states

**IDLE.** No session exists and nothing is locked.

- Cameras are open in free-run preview.
- The trigger line is low; in external mode the generator must be off.
- Broadcast, metrics, heartbeat and transfer keep running.
- Leaves on a start request once the preflights pass: config, disk, cameras present,
  board answering, session directory created.

**ARM.** Each acquisition worker runs `StopGrabbing`, writes the trigger nodes,
exposure and gain, then `StartGrabbing`. From that call, the camera's next frame is
block ID 1. Encoders open `.part` files and ffmpeg pipes. Settings are now fixed.

- Leaves when every camera reports armed.
- A camera that misses the deadline aborts the start; there is no late join, because
  a camera armed after the first edge counts from a later pulse.

**BARRIER.** Proves every camera is still at count 0 before the trigger is released.
This is what makes "block ID N is the same pulse on every camera" true.

- Mark each camera's pre-barrier frame count; any non-zero count refuses the start.
- External mode adds a settle wait of max(0.5 s, 5 periods) first.
- Release the trigger:
  - board: serial start, then `RDY`, with one retry, vetoed if any camera has
    counted a frame;
  - external: prompt the operator and wait for the first frame, with a timeout.

**RUN.** Steady capture. Each frame carries block ID, camera timestamp and host
timestamp. Frames go through the ring to the encoder, and a subsample goes to
broadcast. Nothing that touches what is recorded can change.

- A frame lost in transfer is a gap in that camera's block IDs.
- A stalled or disconnected camera is retired, not reopened.
- External mode also ends when every camera stays silent past the end-silence
  threshold.

**STOP.** Stop the trigger, let the cameras starve, and drain. The host never cuts
the cameras off mid-stream.

1. Stop the trigger: board stop with ack; external prompt, silence wait, force after
   30 s.
2. Each grab loop exits on retrieve timeout with its "triggers stopped" flag set,
   under a shared deadline.
3. Acquisition sends an end-of-stream marker through the ring; the encoder flushes
   and closes ffmpeg, which writes `moov`.
4. Only then `StopGrabbing` and back to free-run. Data is saved before the cameras
   are touched.

**FINALIZE.** Turn raw outputs into a checked session record, then hand it off.

- `.part` → `.mp4`; write the sidecars and session metadata (trigger kind, fps,
  arm and barrier times, per-camera totals, retired cameras, settings read back).
- Run the block-ID rate check, gap counts and the pre-barrier zero check; failures
  go into warnings.
- Compute the cross-camera intersection (option A) or leave it to offline alignment.
- Mark the session finalized, queue it for transfer, return to IDLE.

**Failure paths.** Any failure before the trigger is confirmed is a refusal: nothing
is recorded, the session directory is removed, and the cameras go back to preview.
Any failure after it goes through STOP → FINALIZE so whatever was captured is kept
and labeled. The soft and hard drain signals change how fast the drain runs, but
neither skips FINALIZE.

## Block IDs

- Each camera saves the block IDs it actually captured. A transmission loss is a
  hole in that list.
- The **intersection** keeps only the IDs every camera has, so frame k of every
  video is the same instant.
- **Unwrap**: GigE IDs are 16-bit and run 1..65535 then back to 1; count the
  rollovers to get one increasing number.
- **Time from ID**: t = (ID − 1) / fps.
- **Rate check**: a trigger the camera ignored uses no ID and leaves no gap. Compare
  Δdev_ts with ΔID × period to find it.

## Resync after re-arm: deferred

A re-armed camera counts from 1 again. Panopticon recovers the true ID as
`last good ID + Δdev_ts / period`, using the measured period. It accepts the result
only if it lands within 0.25 of a period from a whole number, and otherwise retires
the camera.

The risks are clock drift (~220–250 ppm between the cameras and the board, so
roughly 10 s of gap before a quarter-period error with the nominal period) and
triggers ignored around the stall.

Decision: don't build it at first. Sessions are short; on a stall, retire the
camera, record the gap, and finish the session.

## Integration

- `mode: continuous | synced` in config. `__main__` hands off to the existing
  `Supervisor` (unchanged) or a new `SessionSupervisor`.
- Both share `build()` for rings, queues and the broadcast, transfer and metrics
  workers.
- `SessionSupervisor` holds the `SessionController` state machine. It drives the
  per-camera processes through multiprocessing Events (arm, trigger released, stop)
  and reads per-camera status (armed, frames before barrier, last-frame time,
  retired) from a control queue.
- Mode parameters on existing pieces, so nothing forks:
  - `PylonSource(trigger=True)`
  - `Acquisition` failure policy: reconnect in continuous, retire-and-report in
    synced
  - `Encoder` takes a scheduler object: `ChunkScheduler` or `SessionScheduler`
  - the sidecar writer gains `block_id` and `dev_ts`
- In synced mode the watchdog stays on for transfer, metrics and broadcast, but
  never respawns acquisition or encoder workers mid-session.

## Deployment

- Still one systemd-managed `frameforge` daemon. In synced mode it idles in IDLE
  between sessions, keeping preview, broadcast, metrics, heartbeat and transfer up,
  and avoiding a camera re-open and ForceIp per session.
- Sessions are started and stopped through the rig's own HTTP API
  (`frameforge-api`), which commands the running daemon. There is never one process
  per session. See [REMOTE_MANAGEMENT.md](REMOTE_MANAGEMENT.md).
- On a crash, systemd restarts the daemon into IDLE. An interrupted session is
  finalized as incomplete from its `.part` files.
- A camera can belong to only one process, so continuous and synced never run at
  the same time on one box: one unit with a mode switch, or two units with
  `Conflicts=`.
