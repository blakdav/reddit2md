"""One GPU, several apps: a lock shared across containers.

Vidscribe and reddit2md both run local AI work on the same GPU (and Vidscribe
also transcribes there). Inside one app a thread lock is enough; across
containers they share a lock file instead: GPU_LOCK_FILE, a file on a host
folder mounted into every container that uses the GPU (e.g. /opt/docker/gpu
mounted at /gpu, GPU_LOCK_FILE=/gpu/lock). flock() works across containers on
the same host because they share the kernel. Without GPU_LOCK_FILE this is a
plain thread lock, as before.

The holder writes its name into the file, so an app that's waiting can show
who has the GPU. Same file in Vidscribe, reddit2md and TubeStreamer; keep
them in sync.
"""

import fcntl
import logging
import os
import threading
import time

log = logging.getLogger("gpulock")


class GpuLock:
    def __init__(self, name, path=None):
        self.name = name
        self.path = path if path is not None else os.environ.get("GPU_LOCK_FILE", "").strip()
        self._thread = threading.Lock()
        self._fd = None

    def _open(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                os.fchmod(fd, 0o666)  # whichever app creates it, the others (maybe other users) can use it
            except OSError:
                pass
            return fd
        except PermissionError:
            pass
        try:
            # Created by another user without write access for us: flock() still
            # works on a read-only handle; we just can't write our name into it.
            return os.open(self.path, os.O_RDONLY)
        except OSError as e:
            log.warning("GPU lock file %s unusable (%s); not sharing the GPU lock", self.path, e)
            return None

    def acquire(self, detail=""):
        self._thread.acquire()
        if not self.path:
            return True
        fd = self._open()
        if fd is None:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # waits while another app holds it
        except OSError as e:
            log.warning("GPU lock failed (%s); continuing without it", e)
            os.close(fd)
            return True
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{self.name}{': ' + detail if detail else ''}\n{int(time.time())}\n".encode(), 0)
        except OSError:
            pass  # read-only handle: locked, just unnamed
        self._fd = fd
        return True

    def try_acquire(self, detail=""):
        """Take the lock only if it's free right now. Returns True if taken
        (then release() it), False if another thread or app holds it."""
        if not self._thread.acquire(blocking=False):
            return False
        if not self.path:
            return True
        fd = self._open()
        if fd is None:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            self._thread.release()
            return False
        except OSError as e:
            log.warning("GPU lock failed (%s); continuing without it", e)
            os.close(fd)
            return True
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{self.name}{': ' + detail if detail else ''}\n{int(time.time())}\n".encode(), 0)
        except OSError:
            pass
        self._fd = fd
        return True

    def release(self):
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                try:
                    os.ftruncate(fd, 0)
                except OSError:
                    pass
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._thread.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def holder(self):
        """Who holds the GPU right now in another app (e.g. "Vidscribe: transcribing"), or None.
        Only meaningful while this app doesn't hold it itself."""
        if not self.path or self._fd is not None:
            return None
        fd = self._open()
        if fd is None:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None  # free
        except BlockingIOError:
            text = os.pread(fd, 200, 0).decode(errors="replace").split("\n")[0].strip()
            return text or "another app"
        finally:
            os.close(fd)
