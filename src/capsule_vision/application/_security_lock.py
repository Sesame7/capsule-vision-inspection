"""One OS lease shared by the application and explicit offline maintenance."""

import os
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .identity_models import SecurityBusyError, SecurityStorageError


class SecurityLease:
    def __init__(self, database: Path) -> None:
        self._file: BinaryIO | None = None
        if database.is_symlink():
            raise SecurityStorageError("Security database must not be a symlink")
        self.path = database.resolve().with_suffix(".lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise SecurityStorageError("Security lock must not be a symlink")
            stream = self.path.open("a+b")
        except OSError as exc:
            raise SecurityStorageError(
                "Cannot open local security ownership lock"
            ) from exc
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise SecurityBusyError(
                "Security storage is in use; stop the application and other maintenance first"
            ) from exc
        self._file = stream

    def close(self) -> None:
        stream, self._file = self._file, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> SecurityLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
