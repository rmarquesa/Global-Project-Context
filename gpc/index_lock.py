"""Nonblocking repository locks (POSIX flock and Windows msvcrt).

Lock files contain no source content and are never unlinked: their stable inode
keeps waiting and newly arriving processes on the same lock. Existing unsafe
paths are rejected; this is not a defense against malicious concurrent renames.
The OS releases locks when a process exits, including abnormal termination.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
from typing import Iterator


class RepositoryBusyError(RuntimeError):
    """Another operation holds this repository's lock."""


@contextmanager
def repository_lock(root_path: str | Path, *, name: str = "index") -> Iterator[None]:
    """Hold a repository-local lock, raising RepositoryBusyError immediately if busy."""
    if name not in {"index", "watch"}:
        raise ValueError("Repository lock name must be 'index' or 'watch'")
    root = Path(root_path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    metadata = root / ".gpc"
    metadata.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(metadata.lstat().st_mode):
        raise ValueError(f"Unsafe repository metadata directory: {metadata}")

    directory_fd = None
    fd = None
    acquired = False
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        filename = f"{name}.lock"
        lock_path = metadata / filename
        try:
            existing = lock_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise ValueError(f"Unsafe repository lock file: {lock_path}")
        if os.name == "posix":
            directory_fd = os.open(
                metadata,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        elif os.name == "nt":
            fd = os.open(lock_path, flags | os.O_BINARY, 0o600)
        else:
            raise RuntimeError(f"Repository locks are unsupported on {os.name}")
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Unsafe repository lock file: {lock_path}")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RepositoryBusyError(
                    f"Repository {name} is busy: {root}; retry after the active operation finishes"
                ) from exc
            raise
        yield
    finally:
        try:
            if acquired:
                if os.name == "posix":
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            if fd is not None:
                os.close(fd)
            if directory_fd is not None:
                os.close(directory_fd)
