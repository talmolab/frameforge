# Live broadcasting

Parallel live stream branched off acquisition for operator viewing, never jeopardizing recording. Implemented 2026-06-04 on Jetson, MS-01-aware.

---

## Hard constraints (locked)

1. **Recording must NEVER suffer for broadcast.** Broadcast may drop frames freely; recording may not.
2. **Recording uses one encoder block; broadcast uses the other.** Jetson: NVENC HW for recording, libx264 CPU for broadcast. MS-01: hevc/h264 QSV for one, libx264 CPU for the other (TBD per backend benchmark).
3. **Broadcast is LAN-only.** Same subnet as MS-01 / Jetson; no internet egress, no auth needed beyond existing network policy.

---

## TL;DR

- **Per-camera broadcast ring** (4 slots full-resolution, ~5 MB per cam). Acq writes here at 10 fps (skip 4 of every 5 frames). Separate from recording ring; no slot-ownership race.
- **Per-camera broadcast worker** (`bcast:cam_XX`). Reads slot index from `broadcast_queue`, copies frame, runs through GStreamer pipeline, releases slot.
- **GStreamer pipeline**: downscale to 640×512, burn in `cam_XX` label + clock, encode at 200 kbps, push to `rtsp://127.0.0.1:8554/<cam_id>` via `rtspclientsink`. `queue leaky=2` element between encoder and sink ensures no buildup regardless of viewer state.
- **RTSP server (separate process)**: MediaMTX on the same host. Web UI at `http://host:8888/` shows mount-point list and plays each via HLS in-browser. Direct RTSP URL works in VLC / mpv / ffplay.
- **No happy-path counters**: only `bcast.<cam>.dropped` (failure) + `bcast.<cam>.encode_ms_max` (perf) + `bcast.session_alive` (gauge).

---

## Architecture

```
acq:cam_08
  │
  ├── recording ring (128 slots, 1.3 MB each) ──► data_queue ──► enc:cam_08 ──► .mp4
  │
  └── broadcast ring (4 slots, 1.3 MB each) ──► broadcast_queue ──► bcast:cam_08
                                                                       │
                                                                       ▼
                                                         GStreamer pipeline
                                                         (libx264 ultrafast)
                                                                       │
                                                                       ▼
                                                         rtspclientsink → MediaMTX
                                                                       │
                                                                       ▼
                                                         RTSP: rtsp://host:8554/cam_08
                                                         HLS:  http://host:8888/cam_08
```

### Why a separate broadcast ring (not a tee on the recording ring)

The recording encoder owns ring-slot lifecycle: it acquires a slot when it processes a frame and releases it back to the free list when done. If broadcast also reads from that slot, there's a race: the encoder might release (slot reused by acq) before broadcast reads.

Solutions considered:
- **Shared ring + broadcast copies bytes out immediately after `get`**: still a race — between broadcast's `get` and its memcpy, encoder might already have released the slot.
- **Refcounted slots (both consumers must release)**: adds coordination state in the ring; new failure modes.
- **Separate broadcast ring written by acq at frame time**: **chosen.** Acq does two memcpys per kept frame (recording ring + broadcast ring). No coordination, no race, simple.

### Cost of the extra acq memcpy

At 50 fps × 1.3 MB and 10 fps subsample (1 in 5), the extra memcpy is ~100 µs every 5 frames = ~20 µs averaged across the loop. The acq loop budget is 20 ms (20,000 µs); the broadcast cost is 0.1% of budget. `acq_loop_ms_max` will surface any actual spike.

---

## GStreamer pipeline (per camera, libx264 path)

```
appsrc ! video/x-raw,format=GRAY8,width=1280,height=1024,framerate=10/1
  ! videoconvert ! videoscale
  ! video/x-raw,format=I420,width=640,height=512
  ! textoverlay text=cam_08 valignment=top halignment=left font-desc="Sans 16"
  ! clockoverlay valignment=top halignment=right font-desc="Sans 16" time-format="%Y-%m-%d %H:%M:%S"
  ! x264enc speed-preset=superfast tune=zerolatency pass=qual quantizer=23
  ! h264parse
  ! queue leaky=2 max-size-buffers=4 max-size-time=0 max-size-bytes=0
  ! rtph264pay
  ! rtspclientsink location=rtsp://127.0.0.1:8554/cam_08
```

**CRF (constant quality) over bitrate**: matches the campy baseline `-c:v libx264 -crf 23 -preset superfast -pix_fmt yuv420p`. CRF lets libx264 spend bits where they matter; for a mostly-static cage view, the resulting bitrate runs well below a fixed cap and quality is consistent at scene changes.

**GRAY8 source**: cameras are mono; sending GRAY8 directly to GStreamer (with `cv2.VideoWriter(..., isColor=False)`) skips a `cv2.cvtColor(GRAY→BGR)` step in the broadcast worker and lets libx264 see honest grayscale. The I420 mid-pipeline has chroma planes set to neutral; x264 compresses them to near-nothing — the "gray specificity" advantage of libx264.

**`queue leaky=2 max-size-buffers=4`** is the no-buildup guarantee. If downstream (RTSP / network / viewer) is slow, oldest buffers are dropped at this queue; broadcast worker keeps pushing new frames. Producer never blocks.

**Burn-in**: `textoverlay` adds cam id; `clockoverlay` adds wall-clock timestamp. Both happen post-downscale (cheaper).

**HW path (MS-01)**: replace `x264enc` with `vaapih264enc rate-control=cqp` (constant quantizer mode for QSV) or `nvv4l2h264enc preset=2 bitrate=200000` for NVIDIA NVENC. Same wrapping structure; only the encoder element changes. (NVENC doesn't have true CRF; falls back to bitrate target.)

---

## RTSP server: MediaMTX

External component, separate systemd unit. Default config:
- RTSP listener: `0.0.0.0:8554`
- HLS listener: `0.0.0.0:8888`
- Publishers (our `rtspclientsink`): connect to push streams
- Viewers: connect to pull streams

Install (covered in Topic 9):
- Manual now: download binary + write a small `mediamtx.service` systemd unit
- Future: `docker-compose.yml` running both frameforge and mediamtx so `systemctl start frameforge` brings the whole stack up

URLs:
- `rtsp://<host>:8554/cam_08` — VLC, mpv, ffplay
- `http://<host>:8888/` — MediaMTX's built-in web UI listing all mount points
- `http://<host>:8888/cam_08` — single-stream HLS view in any browser

Dashboard for per-box overview = MediaMTX's web UI. Add custom HTML on top later if needed.

---

## Config

```yaml
broadcast:
  enabled: true         # default true; flip to false to disable broadcast workers globally
  backend: libx264      # libx264 | hevc_qsv | nvv4l2h264enc (MS-01 backends to add)
  crf: 23               # libx264 quality (0..51 valid; 18..28 sane; default 23 matches campy baseline)
  preset: superfast     # x264enc speed-preset
  rtsp_host: 127.0.0.1  # MediaMTX address; default loopback
  rtsp_port: 8554
```

Hardcoded (no config knobs):
- Broadcast subsample rate: every 5th frame (10 fps target from 50 fps)
- Downscale resolution: 640×512
- Text overlay: cam id + clock, both always on
- GStreamer queue: `leaky=2 max-size-buffers=4`
- Broadcast ring size: 4 slots
- Broadcast queue depth: 8

User can override `crf`, `preset`, `backend`, and the RTSP target. Everything else falls out of the design.

---

## Worker layout (post-change)

| Worker | Spawned when |
| --- | --- |
| `acq:cam_XX` | Always (one per camera) |
| `enc:cam_XX` | Always (one per camera) |
| `bcast:cam_XX` | Only when `broadcast.enabled: true` (one per camera) |
| `transfer` | Always |
| `metrics` | Always |
| `host_sampler` | Always |

For 1 camera + broadcast enabled: 6 workers. For 8 cameras + broadcast: 27 workers (8 × 3 + 3 shared).

---

## Metrics

| Key | Type | Label | Purpose |
| --- | --- | --- | --- |
| `bcast.<cam>.dropped` | Counter | cam | Frame dropped because broadcast ring or queue was full at acq side. **Failure counter (kept).** |
| `bcast.<cam>.encode_ms_max` | Gauge | cam | Per-sample-window max encode time. Promoted to Histogram post `prometheus_client` migration. |
| `bcast.session_alive` | Gauge | (none) | 0/1 — RTSP pipeline opened successfully. 0 means MediaMTX unreachable or pipeline failed; supervisor watchdog will respawn the worker. |

Explicitly **not** in the set per the locked metric philosophy:
- ~~`bcast.<cam>.frames_sent`~~ — happy-path counter
- ~~`bcast.<cam>.viewers`~~ — happy-path gauge

---

## Failure modes

| Failure | Behavior |
| --- | --- |
| MediaMTX not running | `backend.open()` raises; worker logs error + sets `bcast.session_alive = 0` + exits. Supervisor respawns with exponential backoff (cap 30 s). Once MediaMTX comes up, the next respawn succeeds. |
| Broadcast worker crashes | Supervisor respawns. Recording untouched. RTSP viewers see brief disconnect, reconnect on next worker start. |
| Broadcast ring full at acq (worker slow) | `acq._tee_broadcast` catches `queue.Empty`, increments `bcast.<cam>.dropped`. Recording continues. |
| Broadcast queue full at acq (worker fell way behind) | Same as above — slot is released; counter increments. |
| GStreamer pipeline fails mid-stream | `backend.write` returns False; worker continues attempting to write; if persistent, recording keeps going while this worker quietly drops. (Could escalate to a metric / exit + respawn — not done yet; revisit if observed in testing.) |
| Network upstairs unplugged | RTSP viewers disconnect; broadcast pipeline keeps running and dropping buffers at the leaky queue. Recording untouched. |

---

## Multi-cam scaling notes (MS-01-aware)

### CPU (libx264)
- ~5–10% of one P-core per stream at 640×512 × 10 fps
- 8 cams: ~50–80% of one P-core, or distributed across the i9-13900H's 6 P + 8 E cores
- Plenty of headroom

### Intel iGPU (if QSV)
- ~8 simultaneous H.264 sessions, ~4–6 HEVC on Xe iGPU
- If recording uses QSV (8 cams × H.264 = 8 sessions), broadcast MUST use libx264 (CPU) to avoid contention
- Bandwidth.md note added with this constraint

### Network
- 8 cams × 200 kbps = 1.6 Mbps total broadcast bandwidth
- 2.5G office NIC: 0.06% utilization

### NVMe
- Broadcast writes zero bytes to disk. No contention with recording.

### Memory
- 8 cams × 4 slots × 1.3 MB = ~42 MB broadcast ring total. Trivial.

### CPU affinity (deferred to MS-01 deploy)
- Acq + recording encoder on P-cores (latency-critical)
- Broadcast + transfer + metrics on E-cores
- Via systemd `CPUAffinity=` directives. Not in this code change.

---

## Code structure (implemented)

| File | Change |
| --- | --- |
| `frameforge/config.py` | `BroadcastCfg` dataclass; validation that `bitrate_kbps > 0` when enabled |
| `frameforge/acquisition.py` | `__init__` accepts optional `broadcast_ring` + `broadcast_queue`; new `_tee_broadcast()` method called every 5th frame |
| `frameforge/broadcast.py` | New: `BroadcastBackend` ABC, `GStreamerLibx264Backend`, `Broadcast` worker class, `make_broadcast_backend()` factory |
| `frameforge/supervisor.py` | When `broadcast.enabled`: per-cam broadcast ring (4 slots) + queue (depth 8); spawn `Broadcast` worker per cam |
| `config/bench.yaml` | `broadcast.enabled: true` for Jetson |

No new dependencies. Reuses existing `cv2.VideoWriter` + GStreamer stack already in place for recording.

---

## What's NOT in this pass

- **MediaMTX install** — covered in Topic 9 (testing + deploy)
- **CPU affinity** — deferred to MS-01
- **`hevc_qsv` / `nvv4l2h264enc` broadcast backends** — MS-01-specific; add when MS-01 lands
- **Custom dashboard HTML** — MediaMTX's built-in web UI handles per-box overview
- **Auth on RTSP** — LAN-only by design; rely on network ACL

---

## Verification (manual)

After implementing + installing MediaMTX:
1. `systemctl start mediamtx frameforge` (in that order)
2. `journalctl -u frameforge | grep "broadcast cam_08 starting"` — confirm worker spawned
3. `curl -s localhost:8888/v3/paths/list` — confirm MediaMTX sees the mount point
4. From a viewer machine: `vlc rtsp://<host>:8554/cam_08` or open `http://<host>:8888/` in a browser
5. Check `curl -s localhost:9100/metrics | grep bcast` — confirm `bcast_session_alive == 1`, `bcast_dropped == 0`
