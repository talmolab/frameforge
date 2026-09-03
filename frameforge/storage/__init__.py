"""What the transfer worker needs from any upload destination, plus the
registry of backends. Backends import lazily so the package loads without
their SDKs."""

from typing import Protocol


class StorageBackend(Protocol):
    location: str

    @property
    def alive(self) -> bool: ...

    def ensure_open(self) -> bool: ...

    # Atomic: the file is visible at relative_path only once fully written.
    def put(self, local_path: str, relative_path: str) -> None: ...

    def mark_dead(self) -> None: ...

    def close(self) -> None: ...


_REQUIRED = {
    "smb": {"server", "share", "root"},
    "s3": {"bucket"},
}
_OPTIONAL = {
    "smb": set(),
    "s3": {"prefix", "endpoint_url", "region"},
}

STORAGE_KINDS = tuple(_REQUIRED)


def validate_storage(kind: str, options: dict) -> None:
    if kind not in _REQUIRED:
        raise ValueError(
            f"config: transfer.storage.kind {kind!r} not in {STORAGE_KINDS}")
    missing = _REQUIRED[kind] - set(options)
    if missing:
        raise ValueError(f"config: transfer.storage {kind} needs {sorted(missing)}")
    unknown = set(options) - _REQUIRED[kind] - _OPTIONAL[kind]
    if unknown:
        raise ValueError(f"config: transfer.storage {kind} does not take {sorted(unknown)}")


def make_storage(storage_config) -> StorageBackend:
    kind = storage_config.kind
    if kind == "smb":
        from .smb import SmbStorage
        return SmbStorage(**storage_config.options)
    if kind == "s3":
        from .s3 import S3Storage
        return S3Storage(**storage_config.options)
    raise ValueError(f"unknown storage kind {kind!r}")
