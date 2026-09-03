"""Single source of truth for prom_client metric objects.

Multi-process mode: workers write to mmap files under
``$PROMETHEUS_MULTIPROC_DIR``; the exporter aggregates via
``MultiProcessCollector``. Set ``PROMETHEUS_MULTIPROC_DIR`` before this
module is first imported (handled in ``__main__.py``).
"""

from prometheus_client import Counter, Gauge, Histogram


_ENC_BUCKETS = (0.001, 0.0015, 0.002, 0.003, 0.005, 0.008, 0.010, 0.015, 0.020, 0.035, 1.0)
_ACQ_BUCKETS = (0.015, 0.017, 0.019, 0.020, 0.021, 0.023, 0.025, 0.030, 0.050, 2.0)
_BCAST_BUCKETS = (0.001, 0.002, 0.003, 0.005, 0.01, 0.025, 0.1, 1.0)


# --- Acquisition ---------------------------------------------------------
acq_incomplete = Counter(
    "acq_incomplete", "Frames the camera reported as incomplete", ["cam"])
acq_overrun_drops = Counter(
    "acq_overrun_drops", "Frames dropped because the frame ring was full",
    ["cam"])
acq_missed_frames = Counter(
    "acq_missed_frames",
    "Frames the camera sent that never reached the acq loop (sequence gap)",
    ["cam"])
acq_loop_duration_seconds = Histogram(
    "acq_loop_duration_seconds", "Per-iteration acquisition grab-loop wall time",
    ["cam"], buckets=_ACQ_BUCKETS)
acq_enc_queue_depth = Gauge(
    "acq_enc_queue_depth", "Current acq→enc data-queue depth",
    ["cam"], multiprocess_mode="livesum")
acq_enc_ring_free = Gauge(
    "acq_enc_ring_free", "Free slots remaining in the shared acq+enc frame ring",
    ["cam"], multiprocess_mode="livesum")
acq_camera_alive = Gauge(
    "acq_camera_alive", "1 when the camera source is open, 0 when disconnected",
    ["cam"], multiprocess_mode="livesum")


# --- Encoder -------------------------------------------------------------
enc_writer_failures = Counter(
    "enc_writer_failures", "Encoder backend write failures",
    ["cam"])
enc_open_failures = Counter(
    "enc_open_failures", "Encoder backend open failures",
    ["cam"])
enc_idle = Gauge(
    "enc_idle", "1 when encoder is idling until the next chunk boundary",
    ["cam"], multiprocess_mode="livesum")
enc_encode_duration_seconds = Histogram(
    "enc_encode_duration_seconds", "Per-frame encoder write wall time",
    ["cam"], buckets=_ENC_BUCKETS)


# --- Broadcast -----------------------------------------------------------
broadcast_enabled = Gauge(
    "broadcast_enabled",
    "Count of broadcast workers with an open RTSP publisher (0 = all down)",
    multiprocess_mode="livesum")
bcast_encode_duration_seconds = Histogram(
    "bcast_encode_duration_seconds", "Per-frame broadcast encoder write wall time",
    ["cam"], buckets=_BCAST_BUCKETS)


# --- Transfer ------------------------------------------------------------
transfer_session_alive = Gauge(
    "transfer_session_alive", "1 when SMB session is registered, 0 when down",
    multiprocess_mode="mostrecent")
transfer_uploaded = Counter(
    "transfer_uploaded", "Chunks successfully uploaded to VAST")
transfer_failures = Counter(
    "transfer_failures", "Per-attempt upload failures")
transfer_discarded = Counter(
    "transfer_discarded",
    "Chunks deleted locally after exceeding max upload attempts "
    "(SMB session was up between failures, so failure is file-specific)")
transfer_free_mb = Gauge(
    "transfer_free_mb", "Free space in scratch directory (MB)",
    multiprocess_mode="mostrecent")
transfer_low_disk = Gauge(
    "transfer_low_disk", "1 when scratch free space is below threshold",
    multiprocess_mode="mostrecent")
transfer_session_prefix = Gauge(
    "transfer_session_prefix",
    "SMB destination prefix for this rig (info metric, value always 1)",
    ["prefix"], multiprocess_mode="mostrecent")


# --- Supervisor ----------------------------------------------------------
sv_worker_restarts = Counter(
    "sv_worker_restarts", "Worker process restart events",
    ["worker"])
sv_soft_drain_pending = Gauge(
    "sv_soft_drain_pending", "1 while supervisor is in soft drain (waiting for chunk boundary)",
    multiprocess_mode="livesum")


# --- Per-worker resource samples (from host_sampler) ---------------------
worker_rss_mb = Gauge(
    "worker_rss_mb", "Resident set size per worker (MB)",
    ["worker"], multiprocess_mode="mostrecent")
worker_cpu_user_seconds = Gauge(
    "worker_cpu_user_seconds", "Cumulative user CPU per worker (seconds)",
    ["worker"], multiprocess_mode="mostrecent")
worker_cpu_ratio = Gauge(
    "worker_cpu_ratio",
    "Per-worker CPU utilisation (fraction of one core over last sample interval)",
    ["worker"], multiprocess_mode="mostrecent")


# --- Host sampler --------------------------------------------------------
host_mem_available_mb = Gauge(
    "host_mem_available_mb", "System memory available (MB)",
    multiprocess_mode="mostrecent")
host_load_avg_1m = Gauge(
    "host_load_avg_1m", "1-minute system load average",
    multiprocess_mode="mostrecent")
host_cpu_busy_ratio = Gauge(
    "host_cpu_busy_ratio",
    "Whole-machine CPU busy fraction (0-1) over last sample interval",
    multiprocess_mode="mostrecent")
