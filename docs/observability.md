# Observability audit — metrics + logs

How frameforge reports on itself: every metric, every log call, the implementation that exposes them, the perf/storage cost, and the philosophy that decides what stays.

**No code changes here** — diagnostic / decision doc. Action items at the end get tracked separately.

**Iteration history**: initial audit (2026-06-01) → philosophy feedback (2026-06-02) → this revision.

---

## TL;DR

| Concern | Decision |
| --- | --- |
| **Metric philosophy** | Raw counters (consumer uses `rate()`); strict perf budget (no per-frame writes); exclusive lanes vs logs/alarms |
| **Metric implementation** | Switch to `prometheus_client` multi-process mode (mmap files in `/dev/shm`) — removes Manager fragility, gets native Histograms |
| **Metric count** | 17 application keys (was 27 — pruned synthetic, discarded, _last gauges, low_disk, cpu_user_seconds) |
| **Log format** | `timestamp LEVEL [worker] message key=val key=val` |
| **Log levels** | Strict: INFO = lifecycle only; WARN = recoverable; ERROR = action required |
| **Log throttling** | Always rate-limit anything that can recur at >1/s |
| **Log stream** | Single unified journald stream with `[worker]` tag |
| **MS-01 retention** | 30-day default; 90-day option (~1.4 GB for 8 cams) |

---

## Part 1 — Metrics

### 1.1 Inventory (after pruning)

Counters (raw; consumer derives `rate()` at query time):

| Key | Source | Label | Purpose |
| --- | --- | --- | --- |
| `acq.<cam>.incomplete` | [acquisition.py](../frameforge/acquisition.py) | cam | Frame marked bad by pylon — packet loss signal |
| `acq.<cam>.overrun_drops` | acquisition.py | cam | Slot acquire timed out — back-pressure stress |
| `enc.<cam>.writer_failures` | [encoder.py](../frameforge/encoder.py) | cam | Backend write returned False |
| `enc.<cam>.open_failures` | encoder.py | cam | Backend open raised |
| `transfer.uploaded` | [transfer.py](../frameforge/transfer.py) | (none) | Successful uploads; derive uploads/hr via `rate()` |
| `transfer.failures` | transfer.py | (none) | Failed upload attempts |
| `transfer.stuck` | transfer.py | (none) | Files past `max_attempts_per_chunk` |
| `bcast.<cam>.dropped` | broadcast.py (incremented in acquisition.py) | cam | Frame dropped because broadcast ring/queue was full at acq tee |

Gauges (point-in-time):

| Key | Source | Label | Purpose |
| --- | --- | --- | --- |
| `acq.<cam>.queue_depth` | acquisition.py | cam | Outbound queue depth at sample tick |
| `acq.<cam>.ring_free` | acquisition.py | cam | Free slots in shm ring |
| `transfer.free_mb` | transfer.py | (none) | Scratch free space, in MB. Low-disk inferred externally. |
| `transfer.session_alive` | transfer.py | (none) | 0/1 SMB session state |
| `worker_restarts.<worker>` | [supervisor.py](../frameforge/supervisor.py) | worker | Cumulative restart count (gauge, monotone) |
| `proc.<worker>.rss_bytes` | [host_sampler.py](../frameforge/host_sampler.py) | worker | Per-worker RSS |
| `proc.<worker>.cpu_percent` | host_sampler.py | worker | Per-worker CPU%, derived in sampler |
| `enc.<cam>.idle` | encoder.py | cam | 0/1 — set to 1 during same-hour-restart idle, 0 during normal recording |
| `enc.<cam>.drain_pending` | encoder.py | cam | 0/1 — set to 1 once soft drain begins (waiting for chunk boundary); paired with periodic `soft drain pending` INFO log every 60 s |
| `bcast.<cam>.encode_ms_max` | broadcast.py | cam | Per-window max broadcast encode time (sampled every 50 frames; promoted to histogram post-migration) |
| `bcast.session_alive` | broadcast.py | (none) | 0/1 — RTSP pipeline opened successfully |

Histograms (planned, post-migration to `prometheus_client` multi-process mode):

| Key | Source | Label | Buckets (ms) |
| --- | --- | --- | --- |
| `acq.<cam>.loop_ms` | acquisition.py | cam | (1, 2, 5, 10, 20, 50, 100) |
| `enc.<cam>.encode_ms` | encoder.py | cam | (1, 2, 5, 10, 20, 50, 100) |

Built-in:
- `frameforge_build_info{version=...}` (gauge)
- `frameforge_uptime_seconds` (gauge)

**Total: 16 application keys + 2 histograms + 2 built-ins.** Was 27.

### 1.2 Removed in this revision

| Removed | Reason |
| --- | --- |
| `acq.<cam>.synthetic`, `enc.<cam>.synthetic` | Removed 2026-06-03 — black-frame fill design dropped; disconnects now produce shorter chunks rather than synthetic continuity |
| `enc.<cam>.frames` | Number grows unboundedly; per user judgment, signal-to-noise drops over time. fps already derivable from chunk-finalize cadence. |
| `enc.<cam>.discarded` | Replaced by `enc.<cam>.idle` (0/1 gauge) — per-frame counter dropped; idle gauge surfaces same-hour-restart on the dashboard |
| `acq.<cam>.loop_ms_last`, `enc.<cam>.encode_ms_last` | Per-frame gauge writes violated strict perf budget; replaced by sampled Histograms |
| `acq.<cam>.loop_ms_max`, `enc.<cam>.encode_ms_max` | Window-max workaround; Histograms give p50/p95/p99 natively |
| `transfer.low_disk` | Derivable from `transfer.free_mb` + threshold at query time |
| `proc.<worker>.cpu_user_seconds` | Replaced by `cpu_percent` (sampler derives the delta) |

### 1.3 Additions to queue

| Addition | Type | Why |
| --- | --- | --- |
| `enc.<cam>.chunk_actual_vs_target` | Gauge | Set at chunk finalize: `frames_written / target`. Direct under-delivery signal. |
| `transfer.pending_files` | Gauge | Count of `.mp4` waiting in scratch. Surfaces queueing during VAST stalls. |
| `acq.<cam>.timestamp_drift_ms` | Gauge | `(host_mono_ns - hw_ts_ns)` skew. Sampled at tick. Diagnoses camera clock drift. |
| `proc.<worker>.cpu_percent` | Gauge | Already listed above; surfaced from sampler instead of consumer-derived. |

Removed from earlier additions list per feedback:
- ~~`chunks_finalized`~~ — overlaps with `transfer.uploaded`
- ~~`packet_resend_requests`~~ — not needed

### 1.4 Philosophy reference (locked 2026-06-02)

Rules applied throughout the inventory:

1. **Purpose**: each metric serves all four use cases (diagnose / trend / dashboard / alert). Schema stays stable.
2. **Counters stay raw**: standard Prometheus idiom; consumer derives `rate(enc_frames_total[5m])` for fps. Counters grow forever — fine, float64 precision holds for centuries at our scale.
3. **Strict perf budget**: no per-frame writes. Sample at boundary (every Nth frame or every Ns); `.observe()` at sample tick.
4. **Exclusive lanes** (vs logs/alarms): metric = quantitative trend; log = narrative event with context; alarm = threshold action required. No duplicated signals.

The exclusive-lanes rule retroactively reshapes the log audit (§2.1).

### 1.5 Implementation evaluation

**Current pattern** ([context.py — `MetricsRegistry`](../frameforge/context.py)):
- Two `multiprocessing.Manager().dict()` instances (counters + gauges) shared across workers.
- Workers call `metrics.incr(key, by)` / `metrics.gauge(key, value)`.
- Defensive try/except wrappers swallow errors.
- `_RegistryCollector` in [metrics.py](../frameforge/metrics.py) reads snapshots on each scrape and parses keys into Prometheus families.

**Cons**:
1. **Manager fragility**: separate subprocess holding the dicts. Observed dead after ~15h uptime in prior runs.
2. **IPC overhead per write**: each dict update is a socket round-trip (~50µs). Acceptable when sample-bounded; would be a problem if we ever needed per-frame writes (we don't, per strict budget).
3. **Custom key parsing** in `_key_to_metric` has grown organically; brittle.
4. **No native Histogram** — window-max gauges were a workaround.

**Alternative A — `prometheus_client` multi-process mode (recommended)**:

`prometheus_client` ships `multiprocess.MultiProcessCollector` for exactly this pattern. Each worker writes to mmap'd files in `PROMETHEUS_MULTIPROC_DIR` (typically `/dev/shm/prometheus`); the collector aggregates at scrape time.

- No Manager subprocess. mmap'd files survive worker death.
- Native Counter / Gauge / Histogram. Declare once: `c = Counter('enc_frames_total', 'frames', ['cam'])`, then `c.labels(cam='cam_08').inc()`.
- Atomic file writes; sub-µs per call.
- Histograms become trivial: `Histogram('enc_encode_ms', 'encode time', ['cam'], buckets=(1,2,5,10,20,50,100))`.

Migration cost (mechanical):
- Set `PROMETHEUS_MULTIPROC_DIR=/dev/shm/frameforge-metrics` in the systemd unit; ensure dir exists.
- Initialize `CollectorRegistry` + `MultiProcessCollector(registry)` in metrics.py.
- Replace `metrics.incr()` → `counter.labels(...).inc()`.
- Replace `metrics.gauge()` → `gauge.labels(...).set()`.
- Replace `_last` / `_max` patterns → sampled `histogram.observe()`.
- Delete `MetricsRegistry`, `_RegistryCollector`, `_key_to_metric`.

**Alternative B — per-worker JSON over stdout + sidecar parser**: decoupled but adds a process and adds latency. Overkill.

**Alternative C — ZMQ pub/sub**: vastly overkill.

### 1.6 Recommendation — metrics

1. Apply pruning (§1.2) and additions (§1.3).
2. Migrate to `prometheus_client` multi-process mode pre-MS-01.
3. Promote `loop_ms` / `encode_ms` to Histograms; sampled `.observe()` at every-Nth-frame boundary.
4. Keep `:9100/metrics` HTTP endpoint shape — only storage layer underneath changes.

---

## Part 2 — Logs

### 2.1 By-worker emission inventory

Every `self.logger.*` call site, with the exclusive-lanes verdict applied.

**acquisition.py** (per-cam process)

| Line | Level | When | Freq | Verdict |
| --- | --- | --- | --- | --- |
| `acquisition X starting` | INFO | startup | 1/run | KEEP — lifecycle |
| `acquisition X stopped` | INFO | shutdown | 1/run | KEEP — lifecycle |
| `cam=X applied .pfs PATH` | INFO | open success | 1/open | KEEP — non-obvious config applied |
| `cam=X open serial=S retrieve_ms=N` | INFO | open success | 1/open | KEEP — lifecycle + diagnostic context |
| `grab loop X started` | INFO | grab start | 1/open | DROP — redundant with "open" line |
| `open failed cam=X err=ERROR` | WARN | disconnect path | 1/disconnect | KEEP — recoverable + context |
| `camera disconnected cam=X reason=REASON` | WARN | disconnect path | 1/disconnect | KEEP — recoverable + reason |
| `incomplete frames cam=X count=N in_last=Ds code=C msg=M` | WARN | rate-limited storm | 1/30s under stress | KEEP — context not in metric (error code) |
| `ring full, dropping frames cam=X count=N in_last=Ds` | WARN | back-pressure rate-limited | 1/30s when stressed | KEEP — burst-aggregated, gone the unbounded version |

**encoder.py** (per-cam process)

| Line | Level | When | Freq | Verdict |
| --- | --- | --- | --- | --- |
| `encoder X starting (session=... recstart=...)` | INFO | startup | 1/run | KEEP |
| `encoder X stopping` | INFO | shutdown | 1/run | KEEP |
| `opened chunk cam=X index=I target=T path=PATH` | INFO | per-chunk open | 1/hr | KEEP — lifecycle |
| `finalized path=PATH frames=N/T` | INFO | per-chunk close | 1/hr | KEEP — lifecycle + diagnostic |
| `backend.open failed cam=X index=I` | ERROR | open failure | rare | KEEP — action required (supervisor respawns) |
| `backend.close failed cam=X index=I` | ERROR | close failure | rare | KEEP |
| `WRITER DIED cam=X index=I frame=N/T err=ERROR` | ERROR | mid-chunk failure | rare | KEEP |
| `rename failed src=PART dst=FINAL` | ERROR | finalize failure | rare | KEEP |
| `encoder idle cam=X index=I file exists, waiting for next chunk` | INFO | same-hour-restart entry | 1/restart | KEEP — lifecycle, paired with idle gauge |
| `encoder resumed cam=X` | INFO | idle exit | 1/restart | KEEP — lifecycle |

**transfer.py** (single process)

| Line | Level | When | Freq | Verdict |
| --- | --- | --- | --- | --- |
| `transfer starting (target=//srv/share/root)` | INFO | startup | 1/run | KEEP |
| `transfer stopping` | INFO | shutdown | 1/run | KEEP |
| `SMB session registered to HOST` | INFO | session connect | 1/connect | KEEP — lifecycle |
| `uploaded PATH` | INFO | per-file success | 8/hr | **DEMOTE to DEBUG** — every upload bumps `transfer.uploaded`; line carries no context the counter lacks (exclusive lanes) |
| `could not delete uploaded PATH` | ERROR | post-upload | rare | KEEP — action required |
| `upload failed (attempt N/M): PATH (ERROR)` | WARN | per-attempt | up to 30/file | RATE-LIMIT — log first + every 5th + final |
| `STUCK chunk after N attempts: PATH (ERROR)` | ERROR | once per stuck file | 1/file | KEEP — action required, context-rich |
| `LOW DISK: N bytes free on PATH (threshold=M MB)` | ERROR | per-tick under threshold | up to every 30s | RATE-LIMIT — log first + every 10 min |
| `SMB session registration failed: ERROR` | ERROR | per-reconnect attempt | 1/scan when down | RATE-LIMIT — log first + every 10 min while down |
| `remote makedirs failed (PATH): ERROR` | WARN | session connect | rare | KEEP |

**supervisor.py** (main process)

| Line | Level | When | Freq | Verdict |
| --- | --- | --- | --- | --- |
| `session=NAME recording_start=TS` | INFO | startup | 1/run | KEEP |
| `starting N workers (M cameras)` | INFO | startup | 1/run | KEEP |
| `started NAME pid=P` / `respawned NAME pid=P` | INFO | per-worker spawn | N+restarts | KEEP — lifecycle |
| `signal SIG received -> draining` | INFO | shutdown | 1/run | KEEP |
| `worker NAME died (restart #R); respawning` | WARN | worker death | rare | KEEP — recoverable + context |
| `force-terminating NAME` | WARN | drain timeout | rare | KEEP |
| `draining: waiting up to Ns for workers to finalize` | INFO | shutdown | 1/run | KEEP |
| `supervisor exit` | INFO | shutdown | 1/run | KEEP |

**metrics.py / host_sampler.py**

| Line | Level | When | Freq | Verdict |
| --- | --- | --- | --- | --- |
| `metrics exporter listening on :9100/metrics` | INFO | startup | 1/run | KEEP |
| `scrape failed: ERROR` | WARN | per-scrape error | rare | KEEP |
| `key parse failed (KEY): ERROR` | WARN | per-key error | rare | REMOVE — disappears with `prometheus_client` migration |
| `host sampler starting / stopping` | INFO | lifecycle | 1/run | KEEP |

(`eventbus.py` deleted in Topic 4 — stub had no producer or consumer.)

**Summary of log changes**:
- **DROP**: `grab loop X started`, `DISCARD MODE ...`, `key parse failed ...`
- **DEMOTE INFO→DEBUG**: `uploaded PATH`
- **ADD RATE-LIMIT**: `ring full -> dropped`, `upload failed (attempt N/M)`, `LOW DISK`, `SMB session registration failed`

### 2.2 Format standard

**Canonical template**:
```
2026-06-02T18:14:32Z LEVEL [worker] message key=val key=val
```

- **Timestamp**: ISO 8601 (UTC for now; revisited in topic 3).
- **Level**: 5-char fixed-width: `INFO `, `WARN `, `ERROR`, `DEBUG`.
- **Worker**: short tag (`acq:cam_08`, `enc:cam_08`, `transfer`, `metrics`, `host_sampler`, `supervisor`).
- **Message**: short prose phrase.
- **Suffix**: space-separated `key=val`. Quote values containing spaces.

**Example lines**:
```
2026-06-02T18:14:32Z INFO  [enc:cam_08] chunk opened idx=3 target=180000 path=/scratch/sess/cam_08/.../cam_08.03.mp4.part
2026-06-02T18:14:33Z WARN  [acq:cam_08] incomplete frames burst count=247 in_last=30s latest_code=0xe1010050 latest_msg="payload buffer overrun"
2026-06-02T18:14:34Z ERROR [transfer] stuck file path=/scratch/.../cam_03.05.mp4 attempts=30 last_error="connection reset"
```

**Logger setup recommendation** (in main entry point):
```python
import logging, os, sys

class WorkerFormatter(logging.Formatter):
    def format(self, record):
        worker = getattr(record, "worker", "main")
        return "%s %-5s [%s] %s" % (
            self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            record.levelname,
            worker,
            record.getMessage(),
        )

handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(WorkerFormatter())
logging.basicConfig(level=os.environ.get("FF_LOG_LEVEL", "INFO"), handlers=[handler])
```

Workers attach the `[worker]` tag via a `LoggerAdapter` or contextual filter — `logger = logging.LoggerAdapter(base, {"worker": "acq:cam_08"})`. Detailed implementation pattern deferred.

### 2.3 Level rules (strict)

- **INFO** — lifecycle only. Worker start/stop, recording session begin/end, chunk open/close, SMB session connect, configuration applied at boot. State transitions in the system.
- **WARN** — recoverable error. Camera disconnect, ring full, incomplete frame burst, upload failure (first/last in a retry sequence), worker death + respawn. Operator should notice retrospectively; no immediate action.
- **ERROR** — action required. STUCK chunk, LOW DISK, writer died, supervisor giving up. Maps 1:1 to Prometheus alert rules.
- **DEBUG** — disabled by default. Reconnect attempts, per-action detail. Enable via `FF_LOG_LEVEL=DEBUG`.

### 2.4 Throttling policy

**Rule**: anything that can recur at >1/s gets the burst-counter pattern from `incomplete`.

Reference pattern:
```python
self._burst_count += 1
self._latest = current_event_context
now = time.monotonic()
if now - self._last_emit >= self._interval_s:
    self.logger.warning(
        "X burst count=%d in_last=%ds latest=%s",
        self._burst_count, int(now - self._last_emit), self._latest)
    self._burst_count = 0
    self._last_emit = now
```

Specific applications (from §2.1 verdicts):
| Line | Current behavior | After throttle |
| --- | --- | --- |
| `incomplete` | Already throttled ✓ | unchanged |
| `ring full -> dropped` | Unbounded | Burst + 30s |
| `upload failed (attempt N/M)` | Per attempt (up to 30/file) | First + every 5th + final |
| `LOW DISK` | Every 30s under threshold | First + every 10 min |
| `SMB session registration failed` | Every scan when down | First + every 10 min while down |

Non-recurring or rare-by-nature events skip throttling.

### 2.5 Performance cost

`logging` module per-call cost:
- Disabled level: ~0.5µs (level check, returns)
- INFO at enabled level + StreamHandler to stderr: ~10-30µs
- WARNING with `%s` substitution: ~15-50µs

At our call rates:
- **Steady state**: lifecycle lines only — chunk open/close (~16/hr/cam), ~50 lines/hr total. Negligible.
- **Hot loops**: zero log calls per frame. ✓ Verified in current code paths.
- **Worst-case storm** (rate-limited): one line per camera per interval. At 8 cams × 30s interval = max 16 lines/min. Aggregate cost <1 ms/min.

Verdict: hot paths free; bounded under stress; non-hot paths negligible.

### 2.6 Destination + rotation

Single unified journald stream. `logging.basicConfig(stream=sys.stderr)`, systemd captures, journald stores.

**MS-01** (`/etc/systemd/journald.conf.d/frameforge.conf`):
```
SystemMaxUse=5G
SystemKeepFree=5G
SystemMaxFileSize=500M
MaxRetentionSec=30day
```

**For 90-day retention**: `MaxRetentionSec=90day`, `SystemMaxUse=10G`. With ~15 MB/day at 8 cams = ~1.4 GB for 90 days. Cheap. Pick based on operational policy, not storage.

Query commands:
- All frameforge logs: `journalctl -u frameforge`
- One camera: `journalctl -u frameforge | grep '\[acq:cam_08\]'`
- Errors only: `journalctl -u frameforge -p err`
- Live tail: `journalctl -fu frameforge`
- Time-bounded: `journalctl -u frameforge --since "1 hour ago"`

### 2.7 Storage projection (8-cam MS-01 steady-state)

Per-hour log volume:
- INFO lifecycle (chunk open + finalize): 16/hr × 8 cams = 128 lines ≈ 30 KB/hr
- Transfer success: 0 KB at INFO (demoted to DEBUG)
- Startup banners: amortized ~0/hr
- Rate-limited WARN/ERROR: ~5/hr typical ≈ 1 KB

**Total: ~30 KB/hr = ~720 KB/day = ~22 MB/month** steady.

Under degraded conditions (recurring disconnects, transfer retries): ~3-5× = up to ~100 MB/month.

journald 5 GB cap = ~5 years of frameforge logs at steady state. Retention policy is the binding constraint, not storage.

### 2.8 Recommendation — logs

1. Add `logging.basicConfig` with the canonical format (§2.2) in main entry.
2. Apply `[worker]` tag via LoggerAdapter / context filter in each worker startup.
3. Drop lines: `grab loop X started`, `DISCARD MODE ...`, `key parse failed ...` (latter goes away with metrics migration).
4. Demote `uploaded PATH` from INFO to DEBUG (exclusive lanes — info is in the counter).
5. Apply rate-limit pattern to `ring full -> dropped`, `upload failed`, `LOW DISK`, `SMB session registration failed`.
6. On MS-01: journald rotation caps + 30-day default retention (90-day if desired).

---

## Combined action items (tracked separately)

**Metrics (pre-MS-01)**:
- [ ] Prune 8 metric keys per §1.2
- [ ] Add 3 new keys per §1.3 (chunk_actual_vs_target, pending_files, timestamp_drift_ms)
- [ ] Rename `transfer.free_bytes` → `transfer.free_mb`
- [ ] Surface `proc.<worker>.cpu_percent` (derived in sampler)
- [ ] Migrate to `prometheus_client` multi-process mode; remove `MetricsRegistry`
- [ ] Promote loop_ms / encode_ms to Histograms with sampled `.observe()`

**Logs (pre-MS-01)**:
- [ ] `logging.basicConfig` with canonical format
- [ ] LoggerAdapter wiring for `[worker]` tag per process
- [ ] Drop 3 lines per §2.8.3
- [ ] Demote `uploaded` per §2.8.4
- [ ] Add 4 rate-limits per §2.8.5
- [ ] journald rotation conf + retention policy on MS-01

**Coordination with later topics**:
- Topic 3 (timezone): finalizes timestamp format in §2.2
- Topic 6 (pre-open chunks): resolves "finish chunk on drain" question. (`synthetic` counters and black-frame logs already gone — 2026-06-03 interlude. `enc.<cam>.discarded` and discard-mode log already replaced by `enc.<cam>.idle` gauge + idle/resume INFO lines — 2026-06-04 update.)

**Open meta-question** (already answered for THIS doc — but worth restating in EventBus topic):
- Per exclusive-lanes rule: chunk-finalize is a counter increment + INFO log. NOT an EventBus event unless EventBus has a distinct downstream consumer beyond Prometheus + journald.
- STUCK upload is an ERROR log + counter. Alarming happens in Prometheus rules, not in app code.
