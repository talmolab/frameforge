"""What the transfer worker needs from any upload destination."""

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
