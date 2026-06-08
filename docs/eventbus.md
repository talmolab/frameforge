# EventBus — drop the stub

## Decision (2026-06-03)

Drop `frameforge/eventbus.py` and remove the worker from the supervisor. Worker count goes from N+4 (per-cam acq+enc + transfer + metrics + eventbus + host_sampler) to **N+3**.

## Why

- No producer wired up. No consumer wired up. The worker has been a `time.sleep(5)` loop since the stub was introduced.
- Original aux-input motivation (lever presses, light triggers, behavioral events aligned with frames) has no real hardware source identified for the 8-cam MS-01 deployment.
- Per Topic 2's exclusive-lanes rule, internal app events (chunk-finalize, errors, transfer state) are already covered by metrics + logs. EventBus doesn't earn a third lane.
- A persistent stub costs maintenance attention (mental load, supervisor watchdog noise, hooks during refactor) for zero current value.

## Things that look like they might need EventBus but don't

### Alerting

Alerting is **NOT** EventBus's job. The pattern we already use:
- App emits a metric (counter/gauge) at the relevant error site, plus an ERROR log
- Prometheus scrapes `:9100/metrics`
- **Alertmanager** evaluates threshold rules (`transfer_stuck > 0 for 10m`, `transfer_session_alive == 0 for 5m`, etc.) and fires the notification (Slack / email / PagerDuty)
- Alert rules live in the monitoring repo, not in frameforge

Inline `# Prometheus alert: <expr>` comments are already in transfer.py at each alertable error site (STUCK, LOW DISK, SMB session failed). When MS-01 lands, the corresponding rules go into the monitoring config. No in-process bus required.

### Dashboards

Dashboards (Grafana) scrape **the Prometheus endpoint** at `:9100/metrics` — they consume metrics directly, not events. No EventBus path.

### Broadcasting / preview tee (Topic 7, MS-01-targeted)

Topic 7 will design broadcasting **specifically for the MS-01**:
- Intel iGPU (`hevc_qsv` / `h264_qsv`) for hardware-accelerated second stream — same silicon as the recording encoder
- Or CPU `libx264 -preset ultrafast -tune zerolatency` if hw is reserved for recording
- GStreamer `tee` element branched off acquisition's shared-memory ring, NOT off the recording data_queue — broadcast must NEVER jeopardize recording
- Drop-tolerant by design (skip frames if broadcast can't keep up; recording continues clean)
- Output path: RTSP / WebRTC over the office-uplink NIC (2.5G RJ45), keeping the SFP+ camera ingress lane clean

None of that touches an in-process Python event bus.

## Things that genuinely might motivate something bus-like

Each gets its own purpose-built seam, not a generic pub/sub:

1. **Aux input streams** (TTL / GPIO / serial sensors timestamped against video) — when real hardware lands, add a dedicated `aux_input.py` worker that handles the specific protocol and writes to a sidecar metadata file or a small Counter in metrics. The "bus" for this is just: another worker reading from its source and writing to a known sink. No generic abstraction needed.
2. **Bidirectional integration with hcm-core experiment scheduler** — if hcm-core needs to push events INTO a running frameforge (e.g. "start a labeled session marker now"), the right mechanism is a small HTTP control endpoint or systemd signal, not a multiprocess Python bus.
3. **Code deployments / config push** — out of scope for application code. Solved by systemd + git pull + restart, or later by container orchestration. Not an in-process bus.

## Re-evaluation triggers

Re-open this decision if/when:
- Real aux-input hardware lands (specific protocol identified — TTL/GPIO/serial)
- hcm-core requires bidirectional push notifications and HTTP control doesn't fit
- Some other use case shows up that genuinely *needs* multi-producer / multi-consumer in-process fan-out

Until then: simpler is better. Alerting (Prometheus + Alertmanager), dashboards (Grafana scrape), and broadcasting (Topic 7, MS-01-targeted GStreamer tee) all have their own dedicated infrastructure that doesn't pass through an app-level bus.
