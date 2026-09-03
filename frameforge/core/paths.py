"""System paths. Defaults match the systemd deployment; FF_* env overrides
let the pipeline run from a checkout."""

import os

SCRATCH_DIR = os.environ.get("FF_SCRATCH_DIR", "/var/lib/frameforge/scratch")
PROM_DIR = os.environ.get("FF_PROM_DIR", "/run/frameforge/prom")
CONFIG_DIR = os.environ.get("FF_CONFIG_DIR", "/etc/frameforge")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.yaml")
TENANT_FILE = os.path.join(CONFIG_DIR, "tenant.yaml")
