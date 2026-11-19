"""System paths. Defaults match the systemd deployment; FF_* env overrides
let the pipeline run from a checkout."""

import os

STATE_DIR = os.environ.get("FF_STATE_DIR", "/var/lib/frameforge")
SCRATCH_DIR = os.environ.get("FF_SCRATCH_DIR", os.path.join(STATE_DIR, "scratch"))
DESIRED_FILE = os.path.join(STATE_DIR, "desired.json")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")
HOME_DIR = os.environ.get("FF_HOME", "/usr/local/lib/frameforge")
PROM_DIR = os.environ.get("FF_PROM_DIR", "/run/frameforge/prom")
CONFIG_DIR = os.environ.get("FF_CONFIG_DIR", "/etc/frameforge")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.yaml")
TENANT_FILE = os.path.join(CONFIG_DIR, "tenant.yaml")
