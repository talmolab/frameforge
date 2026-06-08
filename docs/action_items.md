# Action items — consolidated from topics 1 & 2

Generated 2026-06-02 after Topic 1 (bandwidth) and Topic 2 (observability) audits.

Items are partitioned by **when** they can be implemented:
- **NOW** — decisions locked, no dependencies on later topics; safe to apply immediately
- **AFTER TOPIC 3 (timezone)** — depends on timestamp/folder-naming decisions
- **AFTER TOPIC 6 (pre-open chunks)** — depends on discard-mode removal + chunk lifecycle redesign
- **SCHEDULED MIGRATION (metrics)** — `prometheus_client` multi-process mode; large refactor worth its own session
- **MS-01 SETUP** — applies only when production hardware arrives
- **MANUAL / OPS** — not code; runbook entries or one-off scripts

---

## NOW — ready to apply

### Code (low-risk, mechanical)

- [x] **Extract** burst + rate-limit patterns into [log_utils.py](../frameforge/log_utils.py) — `BurstAggregator`, `RateLimited`, `RECURRING_WARN_INTERVAL_S`. Reused across acquisition.py and transfer.py.
- [x] **Drop log line** `grab loop X started` in [acquisition.py](../frameforge/acquisition.py) — redundant with prior `cam=X open` lifecycle line
- [x] **Demote** `uploaded path=...` log from INFO → DEBUG in [transfer.py](../frameforge/transfer.py) — every upload bumps `transfer.uploaded` counter; line carries no extra context (exclusive lanes)
- [x] **Rename** `transfer.free_bytes` → `transfer.free_mb` gauge in transfer.py; math updated to integer MB
- [x] **Rate-limit** `incomplete frames` warning in acquisition.py — migrated inline pattern to `BurstAggregator`
- [x] **Rate-limit** `ring full, dropping frames` in acquisition.py — `BurstAggregator`, mirrors `incomplete` interval
- [x] **Rate-limit** `upload failed` in transfer.py — first attempt + every 5th only; STUCK branch unchanged
- [x] **Rate-limit** `LOW DISK` in transfer.py — `RateLimited(600s)`; resets when above threshold
- [x] **Rate-limit** `SMB session registration failed` in transfer.py — `RateLimited(600s)`; resets on successful register
- [x] **Inline Prometheus alert comments** at error sites: STUCK (`transfer_stuck > 0 for 10m`), LOW DISK (`transfer_free_mb < threshold`), SMB session failed (`transfer_session_alive == 0 for 5m`)

### Docs (already applied this session)
- [x] Remove `enc.<cam>.frames` from observability inventory (per user call)
- [x] Add `enc.<cam>.frames` to pruned-metrics table with rationale

---

## AFTER TOPIC 3 (timezone) — APPLIED

- [x] **Locked timestamp format** to local-time `YYYY-MM-DD HH:MM:SS LEVEL [worker] message` (no brackets around timestamp, no TZ suffix); docs/timestamps.md captures the design
- [x] **Folder naming convention** redesigned: `session_name` boot-stable; `recording_start_str` derived per chunk-open as midnight of current local day; `chunk_index = local_now().hour` (0-23, bounded)
- [x] **Encoder rewritten** to derive folder + index per chunk-open, frame-count primary close + hour-rollover backstop, no monotonic chunk math
- [x] **Transfer rewritten** to drop `_ensure_remote_tree` pre-creation; per-upload `smbclient.makedirs` instead. `_remote_camera_dir` removed (unused)
- [x] **Context slimmed**: dropped `recording_start` and `recording_start_str` fields; only `session_name` remains
- [x] **Supervisor slimmed**: dropped recording_start computations; session_name snapshot only
- [x] **logging_setup updated**: canonical format with `%(processName)s` as the `[worker]` tag — no LoggerAdapter wiring needed because supervisor names processes already

---

## TOPIC 7 (Broadcasting) — APPLIED

Design in [docs/broadcasting.md](broadcasting.md). Implemented on Jetson 2026-06-04.

Final decisions:
- **Frame handoff**: separate per-cam broadcast ring (4 slots), written by acq at frame time (2 memcpys: recording ring + broadcast ring). No shared-ring race. Cost: ~20 µs/frame averaged = 0.1% of 20 ms acq budget.
- 10 fps broadcast (every 5th frame); 640×512 downscale; CRF 23 + superfast preset (matches campy `-c:v libx264 -crf 23 -preset superfast -pix_fmt yuv420p` baseline)
- GRAY8 source straight to GStreamer; broadcast worker doesn't waste a `cv2.cvtColor(GRAY→BGR)`; libx264's empty-chroma compression does the gray-specificity work
- Codec: libx264 superfast (CPU) on Jetson — leaves HW NVENC for recording. MS-01 adds `hevc_qsv` (constant-quantizer mode) / `nvv4l2h264enc` backends later
- **If recording is QSV on MS-01, broadcast MUST be libx264** — Intel Xe iGPU session count
- GStreamer `queue leaky=2 max-size-buffers=4` before RTSP sink — no buildup regardless of viewer state
- Text overlay: cam id (textoverlay) + clock (clockoverlay), both always on
- Protocol: RTSP via MediaMTX (separate process, deferred install to Topic 9)
- Worker `bcast:cam_XX` per camera; opt-in via `broadcast.enabled: true` in YAML (true in bench.yaml for Jetson)
- **Metrics** (no happy-path counters per locked philosophy): `bcast.<cam>.dropped` (counter), `bcast.<cam>.encode_ms_max` (gauge → histogram post-migration), `bcast.session_alive` (gauge). **Dropped from prior list**: `frames_sent`, `viewers`.
- Code structure: `BroadcastBackend` ABC + `GStreamerLibx264Backend` mirrors recording's pattern

Pending (Topic 9 testing): MediaMTX install + systemd unit + RTSP/HLS URL verification.

## TOPIC 6 (Pre-open chunks) — NOT IMPLEMENTING

Decision 2026-06-04: drop pre-open plan. Rationale:
- Today's boundary gap (350–700 ms typical) is well within the ring buffer (128 slots = 2.56 s at 50 fps); no frame loss observed at current scale.
- Cross-day open works without special-case code; `chunk_index` resets to 0 at midnight via TZ-aware math, and `os.makedirs` creates the new day's folder.
- MS-01 i9-13900H HW session limit on Intel Xe iGPU is ~8 H.264 / ~4–6 HEVC simultaneous sessions; 16 simultaneous pipelines (8 cams × 2 each) would likely hit the QSV ceiling. libx264 CPU has no equivalent limit but pre-open's benefit shrinks if libx264 open latency is already small.
- Pre-open's two benefits (decouple open latency from close latency; insulate against multi-cam contention at boundary) only matter if Jetson measurement or MS-01 testing shows actual loss at boundaries. Defer to data.

Re-evaluation trigger: Jetson run with network isolation shows acq overrun or chunk-actual-vs-target shortfall at boundary, OR MS-01 multi-cam test shows transition gaps causing buffer pressure.

## TOPIC 3 REVISION (2026-06-04) — APPLIED

Folded "checklist + naming + chunk_index semantics + idle mode" together:

- [x] **Checklist refinements**: avg loop time < 15 ms target, max < 25 ms ceiling; removed `enc_frames_total`; clarified `transfer_free_mb` ≈ initial except brief dip between chunk finalize and upload
- [x] **`idx` → `index` sweep**: log lines, audit table, checklist, docs
- [x] **`chunk_index` = TZ-aware elapsed hours from today's midnight** (was `local_now().hour`): handles DST sequentially (spring 0-22, fall 0-24, no gap/collision), handles mid-day startup naturally (boot at 14:30 → first file = `cam_08.14.mp4`)
- [x] **Idle mode replaces discard mode**: `_idle_until_next_chunk` with INFO entry/resume logs, `enc.<cam>.idle` 0/1 gauge, no per-frame counter. Drains queue + releases slots so acq isn't artificially back-pressured.
- [x] Removed `enc.<cam>.discarded` metric and `discard mode` WARN log line
- [x] Log cleanups in lines I touched: `FINALIZED` → `finalized`, arrows dropped from key=val context (`-> raising`, `src=X dst=Y`)

## TOPIC 9 (Final testing) — RUNBOOK READY

Doc: [docs/test_runbook.md](test_runbook.md). User runs the 45-min validation on Jetson with isolated camera path (PoE injector → built-in NIC; office traffic on USB3 1G dongle). tc netem skipped — real isolation available.

Validates:
- Service up + all workers spawn
- Prometheus metrics readable at :9100
- Broadcast viewable via VLC / browser at rtsp://<jetson>:8554/cam_08 (or HLS at :8888)
- Hour-boundary chunk rollover works (finalize + new chunk open + upload)
- `acq_incomplete_total` ≈ 0 with isolation (was ~9% shared LAN)
- VAST upload working
- Leave running unattended for week; Mac scraper captures metrics CSV

## TOPIC 8 (Drain + deployment) — CODE APPLIED 2026-06-04 / DOCKER DEFERRED

Doc: [docs/deployment.md](deployment.md). Implemented:

- [x] **Two-signal drain**: SIGTERM (soft, wait for chunk boundary) + SIGINT (hard, immediate). Supervisor installs both handlers.
- [x] **`Context` adds `hard_drain` Event** alongside `drain`. Workers honor whichever applies.
- [x] **Encoder `_record_chunk` inner loop**: checks `hard_drain` only (keeps recording through soft drain until natural close)
- [x] **Encoder `_idle_until_next_chunk`**: honors both (exits on either)
- [x] **Acquisition outer + grab loops + reconnect path**: check `hard_drain`, so frames keep flowing during soft drain
- [x] **Supervisor phased `_shutdown`**: phase 1 waits for encoders to exit at boundary (long timeout); phase 2 terminates remaining workers (acq, transfer, etc.)
- [x] **`_DRAIN_JOIN_SECONDS`** bumped 60 → 3700
- [x] **systemd unit** (`deploy/frameforge.service`): `KillMode=mixed` + `TimeoutStopSec=3700`
- [x] **Soft-drain visibility**: periodic `soft drain pending cam=X frames=N/M eta_s=K` INFO log every 60 s + `enc.<cam>.drain_pending` 0/1 gauge

Deferred (covered in deployment.md, implemented when MS-01 lands):
- Dockerfile + docker-compose.yml + bootstrap.sh
- GitHub Actions image-build workflow for `ghcr.io/talmolab/frameforge`
- `.env.example` per-host config template
- CameraCfg `DeviceUserID` lookup for fleet (current Jetson `CreateFirstDevice` still works)
- Ansible / fleet tooling
- Tailscale

## TOPIC 5 (Testing) — APPLIED

- [x] **Decision: no unit tests, no test framework, no pytest dependency**
- [x] Safety net is staging runs + Prometheus metrics + [docs/testing_checklist.md](testing_checklist.md)
- [x] Checklist covers boot, acq, encoder, on-disk chunks, transfer, logs, drain, cross-day rollover, DST, MS-01 deploy

## TOPIC 4 (EventBus) — APPLIED

- [x] **EventBus stub deleted** ([docs/eventbus.md](eventbus.md) for decision rationale)
- [x] **Worker count** drops from N+4 to N+3
- [x] Alerting confirmed as Prometheus + Alertmanager path (not bus); inline `# Prometheus alert:` comments already at relevant ERROR sites
- [x] Dashboards confirmed as Grafana scrape of `:9100/metrics` (not bus)
- [x] Broadcasting deferred to Topic 7 (MS-01-targeted, GStreamer tee, not bus)
- [x] Re-evaluation triggers documented (real aux-input hardware, hcm-core bidirectional push)

## AFTER TOPIC 6 (pre-open chunks)

- [x] **Removed** `acq.<cam>.synthetic` and `enc.<cam>.synthetic` counters (2026-06-03 interlude: black-frame fill dropped entirely; disconnects produce shorter chunks)
- [x] **Removed** `enc.<cam>.discarded` counter (2026-06-04: replaced by `enc.<cam>.idle` gauge)
- [x] **Removed** `discard mode` WARN log line (2026-06-04: replaced by `encoder idle`/`encoder resumed` INFO pair)
- [x] **Same-hour-restart proposal folded in** (2026-06-04): idle gauge, drain queue + release slots, INFO entry/resume logs
- [ ] **Decide**: finish chunk on drain finish? — superseded by the **drain redesign** entry above; revisit together

---

## SCHEDULED MIGRATION — `prometheus_client` multi-process mode

This is a substantial refactor. Worth its own focused session.

- [ ] Set `PROMETHEUS_MULTIPROC_DIR=/dev/shm/frameforge-metrics` in systemd unit; ensure dir exists at startup
- [ ] Initialize `CollectorRegistry` + `MultiProcessCollector(registry)` in [metrics.py](../frameforge/metrics.py)
- [ ] Replace `metrics.incr()` calls with `counter.labels(...).inc()`
- [ ] Replace `metrics.gauge()` calls with `gauge.labels(...).set()`
- [ ] Promote `loop_ms`/`encode_ms` to native `Histogram` with sampled `.observe()` (buckets: 1, 2, 5, 10, 20, 50, 100 ms)
- [ ] Delete `MetricsRegistry`, `_RegistryCollector`, `_key_to_metric` from [context.py](../frameforge/context.py) and [metrics.py](../frameforge/metrics.py)
- [ ] **Add during migration** (the 3 new keys from §1.3):
  - [ ] `enc.<cam>.chunk_actual_vs_target` (gauge, set at finalize)
  - [ ] `transfer.pending_files` (gauge, sampled at scan)
  - [ ] `acq.<cam>.timestamp_drift_ms` (gauge, sampled at tick)
- [ ] **Surface** `proc.<worker>.cpu_percent` (derive in host_sampler from CPU-time delta)

---

## MS-01 SETUP — when hardware arrives

### Network
- [ ] `sudo ip link set <sfp-iface> mtu 9000` (camera-side SFP+ link)
- [ ] `ethtool -G <sfp-iface> rx 4096` (NIC ring buffer)
- [ ] Enable jumbo frames on the Ubiquiti USW-Enterprise-8-PoE camera ports
- [ ] netplan: static IP + MTU 9000 on SFP+; DHCP on 2.5G office uplink
- [ ] IRQ affinity: pin SFP+ NIC interrupts to dedicated P-cores away from encoder cores

### Kernel
- [ ] `/etc/sysctl.d/99-frameforge.conf`:
  ```
  net.core.rmem_max = 33554432
  net.core.rmem_default = 16777216
  net.core.netdev_max_backlog = 5000
  net.core.netdev_budget = 600
  ```
  Apply with `sudo sysctl --system`.

### journald
- [ ] `/etc/systemd/journald.conf.d/frameforge.conf`:
  ```
  SystemMaxUse=5G
  SystemKeepFree=5G
  SystemMaxFileSize=500M
  MaxRetentionSec=30day
  ```
  (or `MaxRetentionSec=90day` + `SystemMaxUse=10G` if 90-day retention preferred)

### Storage
- [ ] 200–500 GB NVMe partition for scratch (per user choice; 1TB SSD has the room)
- [ ] `low_disk_threshold_mb`: 5000 (bumped from 500 — gives ~8 hrs warning at 8 cams)

### Config
- [ ] prod.yaml: `acq.packet_size: 8192` (assumes MTU 9000)
- [ ] prod.yaml: `acq.inter_packet_delay_ns: 0` initially; tune up if multi-cam `incomplete` rate is nonzero

### Backend
- [ ] Build both encoder backends: `Libx264GrayCRFBackend` and `HevcQsvBackend`
- [ ] Backend selection / fallback logic in factory
- [ ] Install `intel-media-va-driver-non-free`, `vainfo`, `ffmpeg`, `libx264-dev`

### VAST
- [ ] Re-test kernel CIFS mount with `vers=3.1.1` (Jetson's old `cifs.ko` blocker doesn't apply)
- [ ] Decide: kernel CIFS vs userspace `smbprotocol` (currently userspace; works fine)

---

## FUTURE WORK / FOLLOW-UP

- **Real-time anomaly detection on the broadcast lane** (2026-06-05): broadcast ring already gives 10 fps full-resolution mono frames per cam — natural seam for a separate `anomaly:cam_XX` worker that subscribes alongside the broadcast encoder. Detectors range from trivial (pixel-diff, brightness stats — µs/frame) to ML (behavioral models — 10–100 ms/frame, MS-01 iGPU via OpenVINO or Jetson TensorRT). Metrics: `anomaly.<cam>.events_detected` counter + per-type label, `anomaly.<cam>.motion_score`/`brightness`/`frozen` gauges, `anomaly.<cam>.detector_latency_ms` histogram. Recording untouched.
- **Jetson IP discovery fix** (2026-06-05): the `network discovery rabbit hole` from this Friday's testing — ensure avahi-daemon runs on Jetson and mDNS hostname works from Mac on either Wi-Fi or wired switch. Or alternative: bootstrap script posts Jetson IP to a known VAST file on each boot so it's discoverable without scans.

## PARKING LOT (decisions deferred, revisit when relevant)

- **`result.GetTimeStamp()` (hw_ts) visibility** — currently not captured anywhere after the queue-tuple slim (2026-06-03). Open question for Topic 6 / drift-metric work: do we want to keep grabbing it for `acq.<cam>.timestamp_drift_ms`, frame ordering in pre-open-chunks routing, or downstream metadata? If yes, where does it surface (local acq state for the drift metric, queue tuple for encoder routing, sidecar file)? Re-evaluate before adding back to the grab loop.

## MANUAL / OPS

- [ ] **`tc netem` packet-loss test** on Jetson — prove the `acq.incomplete` → kernel-drop chain locally:
  ```bash
  sudo tc qdisc add dev eth0 root netem loss 5%
  # run frameforge, observe acq_incomplete_total rate
  sudo tc qdisc del dev eth0 root
  ```
- [ ] **MTU jumbo-frames test** on Jetson — verify no fragmentation:
  ```bash
  sudo ip link set eth0 mtu 9000
  ping -M do -s 8972 <camera-ip>   # DF bit set; payload = 9000 − 28
  ```
- [ ] **Jetson timezone** — `sudo timedatectl set-timezone America/Los_Angeles` (optional; may be obsoleted by topic 3 code fix)
- [ ] **Prometheus Alertmanager rules** (lives in monitoring repo, not frameforge): alert on `transfer_stuck > 0 for 10m`, `transfer_session_alive == 0 for 5m`, `acq_incomplete rate > 0.01` per cam, `transfer_free_mb < 5000`
- [ ] **Disk cleanup on Jetson** before next run (DKMS detritus already removed; just `~/Projects/frameforge/scratch/` rinse)

---

## How to use this list

Sessions go: pick a topic → audit → make decisions → harvest action items into this list. When ready to implement: pick "NOW" items first; promote items from gated buckets as their gates resolve (topic 3 done → AFTER TOPIC 3 items become NOW).

Items marked `[x]` in NOW have been applied in the same session that produced them.
