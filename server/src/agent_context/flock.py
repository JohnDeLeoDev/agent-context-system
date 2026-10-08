"`flock` for every host this package is imported on, Windows included.\n\nThe relay runs natively on Windows, and it imports `server`, which imports the daemon's modules.\nWindows has no `fcntl`, so those modules take their lock from here: on POSIX this is\n`fcntl.flock` itself, unchanged; on Windows it is `msvcrt.locking` on the file's first byte,\nwhich is enough for the one thing each caller uses it for, a whole-file lock held by one\nprocess at a time. It is mandatory there, not advisory: another process reading that byte\nwhile it is held gets an error. (metrics.py keeps its own guarded fcntl import: the gate\nscripts load it by path, outside this package.) A lock taken with LOCK_NB that another process\nholds raises BlockingIOError, as flock does; a blocking one waits until it is free. Closing the\ndescriptor drops the lock on both."
import os
import time

try:
    from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
except ImportError:                        
    import msvcrt

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8     
    _WAIT_SECONDS = 0.05

    def _region(fd: int, mode: int) -> None:
        'Lock or unlock byte 0 of `fd`, leaving the file position where it was: msvcrt locks\n        from the current position, and a caller may be appending.'
        position = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)

    def flock(fd, operation: int) -> None:
        fd = fd if isinstance(fd, int) else fd.fileno()
        if operation & LOCK_UN:
            _region(fd, msvcrt.LK_UNLCK)
            return
        while True:
            try:
                _region(fd, msvcrt.LK_NBLCK)
                return
            except OSError as exc:
                if operation & LOCK_NB:
                    raise BlockingIOError(*exc.args) from exc
            time.sleep(_WAIT_SECONDS)

__all__ = ["LOCK_EX", "LOCK_NB", "LOCK_UN", "flock"]
