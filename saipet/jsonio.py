"""Atomic JSON persistence for small state files.

One boring reusable helper used by SeenStore, ReviewStore, and anything else
that needs to write a JSON file that must never be half-written. A crash
mid-write leaves either the pre-existing file or the completed new one --
never a truncated mix.

Writer serialisation: an `InterProcessLock` prevents multiple processes from
writing to the same file concurrently. The lock is advisory (best-effort
under crash) and includes staleness detection so a dead holder does not
block indefinitely.

Reader behaviour for malformed files is deliberate: the caller sees a
`StateFileError` with the path and the parse error, never a silent empty
fallback that could lose important information.
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


class StateFileError(ValueError):
    """A state file that exists but is corrupt or unreadable."""


def _lock_path_for(target: Path) -> Path:
    return target.with_name(f".{target.name}.lock")


def pid_alive(pid: int) -> bool:
    """Best-effort process liveness. When uncertain, answer ALIVE: the cost
    of wrongly stealing a live holder's lock (CORE-007) is far higher than
    the cost of waiting one more stale interval on a dead one. Also used by
    durable monitor-status readers to answer "is that daemon still there"
    (W2-002)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "posix":
        import errno

        try:
            os.kill(pid, 0)
        except OSError as exc:
            # ESRCH = no such process; EPERM = exists but owned by another
            # user, which is very much alive.
            return exc.errno == errno.EPERM
        return True
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # A protected but living process denies access instead of
            # vanishing -- that is not the same as "dead".
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # unqueryable -> uncertain -> alive
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- any probing failure means uncertainty
        return True


class InterProcessLock:
    """Cross-process exclusive lock backed by a directory entry.

    The lock file is created with O_CREAT|O_EXCL and records the holder's
    PID plus a per-acquisition owner token. A lock older than `stale_seconds`
    may be broken by the next caller -- but only after the recorded process
    is confirmed dead (CORE-007): a slow, paused or heavily loaded live
    writer must never have its lock stolen.

    Unlock is conditional on ownership: the file is removed only when it
    still carries THIS acquisition's token, so a displaced or expired holder
    cannot delete a successor's lock. The target's parent directory is
    created on demand (W2-008), matching atomic_write_json's path contract.
    Use as a context manager:

        with InterProcessLock(target):
            atomic_write_json(target, value)
    """

    def __init__(self, target: str | Path, stale_seconds: float = 10):
        self._lock_path = _lock_path_for(Path(target))
        self._stale_seconds = stale_seconds
        self._fd = None
        self._token = ""

    @staticmethod
    def _payload(token: str) -> str:
        return f"{os.getpid()} {token}"

    def _read_lock(self) -> tuple[int, str] | None:
        try:
            raw = self._lock_path.read_bytes().decode("utf-8", "replace").strip()
        except OSError:
            return None
        parts = raw.split(None, 1)
        if not parts or not parts[0].isdigit():
            return None
        token = parts[1].strip() if len(parts) > 1 else ""
        return int(parts[0]), token

    def __enter__(self):
        parent = self._lock_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # genuine permission failures surface at os.open below
        deadline = time.time() + max(self._stale_seconds * 2, 0.2)
        while True:
            token = uuid.uuid4().hex
            try:
                self._fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                self._token = token
                os.write(self._fd, self._payload(token).encode("utf-8"))
                return self
            except FileExistsError:
                self._break_if_stale()
                if time.time() > deadline:
                    raise TimeoutError(
                        f"could not acquire lock for {self._lock_path}"
                    )
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        # CORE-007: unlink only what we still own. A successor's replacement
        # lock carries a different token and must survive our exit.
        current = self._read_lock()
        if current is not None and current[1] == self._token:
            try:
                os.remove(self._lock_path)
            except OSError:
                pass
        self._token = ""

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self._lock_path.stat().st_mtime
        except OSError:
            return
        if age <= self._stale_seconds:
            return
        recorded = self._read_lock()
        if recorded is not None and pid_alive(recorded[0]):
            # CORE-007: age alone is not death. A live holder keeps its lock.
            return
        # Delete only the exact entry we judged stale; a successor that
        # replaced it in between carries a different payload.
        if self._read_lock() != recorded:
            return
        try:
            os.remove(self._lock_path)
        except OSError:
            pass


def atomic_write_json(path: str | Path, value) -> Path:
    """Serialize `value` to a temp sibling file, flush, fsync, replace.

    The target directory is created on demand. If the write or the replace
    fails, the temporary file is cleaned up and the original target (if it
    existed) is left intact.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return target


def read_json(path: str | Path):
    """Read a JSON file. Returns None if the file does not exist. Raises
    `StateFileError` on malformed content or if the file exists but cannot
    be decoded -- the caller should report the problem explicitly rather than
    silently substituting an empty state.

    Every plausible read/decoding failure is normalised to StateFileError so
    callers never need to catch bare UnicodeDecodeError, JSONDecodeError or
    OSError.
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise StateFileError(f"{path} is corrupt: {exc}") from exc
