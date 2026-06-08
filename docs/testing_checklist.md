# Manual smoke-test checklist

**Policy (locked 2026-06-03)**: frameforge has no unit tests. The safety net is staging runs + Prometheus metrics + this checklist.

Run before merging to main, after deploying to a new host, or after any change that touches the supervisor, encoder, or transfer paths.

## Boot
- [ ] `systemctl start frameforge` succeeds; `systemctl status frameforge` shows `active (running)`
- [ ] `journalctl -u frameforge | grep started` lists every expected worker: `acq:cam_XX` × N, `enc:cam_XX` × N, `transfer`, `metrics`, `host_sampler`
- [ ] `journalctl -u frameforge -p err --since "5 min ago"` is empty

## Acquisition
- [ ] Log shows `cam=X open serial=...` for every configured camera
- [ ] `curl -s localhost:9100/metrics | grep acq_incomplete_total` shows steady-state rate (low or zero on isolated network)
- [ ] Average loop time < 15 ms (use histogram p50 once `prometheus_client` multi-process mode lands; interim proxy: `acq_loop_ms_max{cam=...}` reads < 25 ms ceiling)
- [ ] `acq_ring_free{cam=...}` > 100; `acq_queue_depth{cam=...}` < 10

## Encoder
- [ ] Log shows `opened chunk cam=X index=N target=180000 path=...` at chunk boundary
- [ ] Encoder keeping up: `acq_queue_depth{cam=...}` < 10 and `acq_ring_free{cam=...}` > 100 (means encoder is draining what acq produces)
- [ ] `enc_writer_failures_total == 0`; `enc_open_failures_total == 0`
- [ ] `enc_idle{cam=...} == 0` during normal recording (1 only if a .mp4 already exists for this chunk after a restart)
- [ ] After chunk rollover: `enc_chunk_actual_vs_target{cam=...}` close to 1.0 (only useful once the metric lands per action_items.md)

## Chunks on disk
- [ ] `ls -lh /var/lib/frameforge/scratch/.../cam_XX/YYYY-MM-DD-00-00-00/` shows `cam_XX.HH.mp4` where HH = TZ-aware elapsed hours from folder's midnight (matches current wall-clock hour on normal days)
- [ ] No `.part` files older than the currently-open chunk

## Transfer
- [ ] `transfer_session_alive == 1`
- [ ] `transfer_uploaded_total` advancing ~1× per cam per hour
- [ ] `transfer_stuck_total == 0`
- [ ] `transfer_free_mb` should sit close to its initial value most of the time; it dips only briefly between chunk finalize and successful upload (~30 s at most) — a sustained downward drift means uploads are falling behind

## Logs
- [ ] `journalctl -u frameforge | head -10` matches canonical format `YYYY-MM-DD HH:MM:SS LEVEL [worker] message`
- [ ] `journalctl -u frameforge | grep '\[acq:cam_08\]'` filters one camera cleanly
- [ ] No WARN/ERROR storms — rate-limited warnings emit at most one per 30 s per camera

## Drain
- [ ] `systemctl stop frameforge` — service exits within `TimeoutStopSec`; in-flight chunks finalize as partials
- [ ] Restart — current chunk_index recomputed from elapsed-hours-since-midnight; if `.mp4` for this index already exists, encoder enters **idle mode** until the next chunk boundary (`enc_idle{cam=...} == 1`), no duplicate-file collision
- [ ] Restart after a longer downtime mid-day: scans folder, opens next chunk at current elapsed-hour index (e.g. boot at 14:30 → first file = `cam_XX.14.mp4`); no default-to-00 behavior

## Cross-day rollover (run only if session spans local midnight)
- [ ] At local midnight: new `2026-MM-DD-00-00-00` folder created under same `session_name`
- [ ] First chunk of new day = `cam_XX.00.mp4`; no collision with previous day's filenames
- [ ] Old day's folder remains as-is; transfer scans both folders and uploads all `.mp4`s

## DST day (only when daylight-saving transition falls inside the session)
- [ ] Spring forward: day produces 23 chunk files (`cam_XX.00.mp4` through `cam_XX.22.mp4`), sequential, no gap. `cam_XX.01.mp4` spans the DST jump (covers 01:00–03:00 wall-clock).
- [ ] Fall back: day produces 25 chunk files (`cam_XX.00.mp4` through `cam_XX.24.mp4`), sequential, no collision. Two passes through 01:00 wall-clock land in separate files.

## After MS-01 deploy (additional)
- [ ] `vainfo` reports Intel iGPU available (only if hevc_qsv backend selected)
- [ ] MTU on camera-side SFP+ link = 9000: `ip link show <sfp> | grep mtu`
- [ ] `ping -M do -s 8972 <camera-ip>` succeeds (no fragmentation across the path)
- [ ] `transfer_free_mb` after first hour ≈ initial − (576 MB × hour) at 8 cams
