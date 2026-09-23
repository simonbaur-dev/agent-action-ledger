"""A small cross-process advisory file lock built only on the standard library.

Two shapes, one implementation::

    with file_lock(path, timeout=10):        # scoped critical section
        ...

    handle = acquire_file_lock(path)         # held across a longer pipeline
    try:
        ...
    finally:
        release_file_lock(handle)

The lock is taken by the operating system (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows), which means the kernel releases it when the
holding process dies. A crash therefore never leaves a permanent exclusion.

The lock file itself is created if missing and then **never deleted**. Deleting
it would let two processes lock two different inodes and enter the critical
section at the same time.

This is an *advisory* lock between cooperating processes on one machine. It is
not a distributed lock and gives no protection on a network filesystem.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Iterator

__all__ = ["LockHandle", "acquire_file_lock", "release_file_lock", "file_lock"]

# EAGAIN, EACCES, and the BSD/macOS EAGAIN/EDEADLK spellings. Only contention
# is retried; any other I/O error is a real failure and propagates.
_RETRYABLE_ERRNO = (11, 13, 35, 36)


class LockHandle:
    """An acquired OS lock. Release it with :func:`release_file_lock`."""

    __slots__ = ("path", "_fh", "held")

    def __init__(self, path: Path | str, fh) -> None:
        self.path = Path(path)
        self._fh = fh
        self.held = True

    def __repr__(self) -> str:
        return f"LockHandle({self.path.name}, held={self.held})"


def acquire_file_lock(path: Path | str, *, timeout: float = 10.0) -> LockHandle:
    """Take the exclusive lock on ``path``.

    The file is created when missing and never removed. Raises
    :class:`TimeoutError` when another live process still holds the lock after
    ``timeout`` seconds (``0`` tries exactly once).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        # msvcrt locks a byte range, so that range has to exist. Append a
        # single byte rather than truncating an existing lock file.
        if os.name == "nt" and path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return LockHandle(path, handle)
            except (BlockingIOError, PermissionError, OSError) as exc:
                if getattr(exc, "errno", None) not in _RETRYABLE_ERRNO:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock busy: {path.name}") from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except BaseException:
        handle.close()
        raise


def release_file_lock(handle: LockHandle | None) -> None:
    """Release and close a handle. Idempotent; a double release never raises."""
    if handle is None or not handle.held:
        return
    handle.held = False
    fh = handle._fh
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        # The process is losing the lock either way: closing the descriptor
        # below drops it. Failing here would mask the caller's own error.
        pass
    finally:
        try:
            fh.close()
        except OSError:
            pass


@contextmanager
def file_lock(path: Path | str, *, timeout: float = 10.0) -> Iterator[None]:
    """Scoped form of :func:`acquire_file_lock`."""
    handle = acquire_file_lock(path, timeout=timeout)
    try:
        yield
    finally:
        release_file_lock(handle)
