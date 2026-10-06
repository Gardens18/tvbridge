"""``fcntl.flock`` on POSIX; the same non-blocking exclusive file lock via ``msvcrt`` on Windows.

Only what tvbridge uses: ``flock(fd, LOCK_EX | LOCK_NB)`` and ``flock(fd, LOCK_UN)``. A busy
lock raises ``OSError(EACCES)`` on Windows, which callers already treat as "held elsewhere".
"""

import os

if os.name == "nt":
    import errno
    import msvcrt

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def flock(fd: int, flags: int) -> None:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if flags & LOCK_UN:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise OSError(errno.EACCES, "lock is held by another process")
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
else:
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock  # noqa: F401
