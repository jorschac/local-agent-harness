"""Conservative local daemon lifecycle; no LaunchAgent and no PID killing.

The server owns owner.lock. CLI offline management acquires the same lock.
Control metadata is advisory: authenticated status must match both the recorded
PID and instance ID before a shutdown request is made.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterator

import httpx


class DaemonError(RuntimeError):
    pass


def owner_lock_path(home: Path) -> Path:
    from . import paths
    helper = getattr(paths, "owner_lock_path", None)
    if callable(helper):
        return Path(helper(home))
    return Path(paths.path_map(home)["owner_lock"])


def token_path(home: Path) -> Path:
    from . import paths
    helper = getattr(paths, "local_token_path", None)
    if callable(helper):
        return Path(helper(home))
    return Path(paths.path_map(home)["local_token"])


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False) -> Iterator[None]:
    """flock without unlinking/replacing the inode on exit."""
    private_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise DaemonError("Owner/control lock is held; use the running service.") from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def owner_is_locked(home: Path) -> bool:
    try:
        with exclusive_lock(owner_lock_path(home)):
            return False
    except DaemonError:
        return True


def offline_dispatch(home: Path, action: str, payload: dict[str, Any]) -> Any:
    """Only call for transport connection failure, never for HTTP errors."""
    import importlib
    management = importlib.import_module("harness.management")
    with exclusive_lock(owner_lock_path(home)):
        offline = getattr(management, "OFFLINE", None)
        token = offline.set(True) if offline is not None else None
        try:
            return management.dispatch(home, action, payload)
        finally:
            if offline is not None:
                offline.reset(token)


def read_local_token(home: Path) -> str:
    try:
        token = token_path(home).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise DaemonError("Local token is missing. Run 'harness init' first.") from exc
    if not token:
        raise DaemonError("Local token is empty; initialization needs repair.")
    return token


def server_url(home: Path) -> str:
    from .paths import load_config
    config = load_config(home)
    server = config.get("server", {})
    if isinstance(server, str):
        return server.rstrip("/")
    host = str(server.get("host", "127.0.0.1"))
    port = int(server.get("port", 8765))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise DaemonError("Daemon control only supports a loopback server host.")
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def _metadata_path(home: Path) -> Path:
    return home / "runtime" / "daemon.json"


def _read_metadata(home: Path) -> dict[str, Any] | None:
    from .paths import path_map
    owner_path = Path(path_map(home)["owner_descriptor"])
    try:
        value = json.loads(_metadata_path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:
            value = json.loads(owner_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise DaemonError("Invalid owner descriptor; refusing unsafe lifecycle operations.") from exc
    except (ValueError, OSError) as exc:
        raise DaemonError("Invalid daemon metadata; refusing unsafe lifecycle operations.") from exc
    if not isinstance(value, dict):
        raise DaemonError("Invalid daemon metadata.")
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise DaemonError("Invalid daemon PID; refusing unsafe lifecycle operations.")
    if value.get("home") != str(home.resolve()):
        raise DaemonError("Daemon record belongs to a different home.")
    if value.get("instance_id") is None:
        # Readiness may time out before the child is reachable. Its own server
        # descriptor can complete that record, but never overwrite an existing ID.
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            owner = {}
        if (isinstance(owner, dict) and owner.get("pid") == pid
                and owner.get("home") == value["home"]
                and isinstance(owner.get("instance_id"), str) and owner["instance_id"]):
            value["instance_id"] = owner["instance_id"]
    return value


def _write_metadata(home: Path, value: dict[str, Any]) -> None:
    path = _metadata_path(home)
    private_directory(path.parent)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    # Signal 0 is an existence check, never a termination signal.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _status(client: httpx.Client) -> dict[str, Any] | None:
    try:
        response = client.get("/api/status")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return None
    if response.status_code != 200:
        raise DaemonError(f"Status verification failed (HTTP {response.status_code}).")
    value = response.json()
    if not isinstance(value, dict):
        raise DaemonError("Invalid service status response.")
    return value


def _matches(record: dict[str, Any], live: dict[str, Any]) -> bool:
    instance = record.get("instance_id")
    return (isinstance(instance, str) and bool(instance)
            and live.get("instance_id") == instance
            and type(live.get("pid")) is int and live["pid"] == record["pid"])


def _client(home: Path, url: str | None, factory: Callable[..., httpx.Client]) -> httpx.Client:
    token = read_local_token(home)
    return factory(base_url=url or server_url(home),
                   headers={"Authorization": f"Bearer {token}"},
                   timeout=5.0, trust_env=False)


def status(home: Path, *, url: str | None = None,
           client_factory: Callable[..., httpx.Client] = httpx.Client) -> dict[str, Any]:
    record = _read_metadata(home)
    with _client(home, url, client_factory) as client:
        live = _status(client)
    locked = owner_is_locked(home)
    if live is not None:
        verified = bool(record and _matches(record, live) and locked)
        return {"state": "running" if verified else "unmanaged", "verified": verified,
                "owner_locked": locked, "service": live}
    alive = bool(record and _pid_alive(record["pid"]))
    return {"state": "unreachable" if locked or alive else "stopped",
            "verified": False, "owner_locked": locked,
            "pid": record.get("pid") if record else None}


def foreground(home: Path) -> None:
    """The server, not this wrapper, acquires and releases the owner lock."""
    os.environ["HARNESS_HOME"] = str(home.resolve())
    from .server import main
    main()


def start(home: Path, *, url: str | None = None, wait: float = 15.0,
          client_factory: Callable[..., httpx.Client] = httpx.Client,
          popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> dict[str, Any]:
    home = home.resolve()
    with exclusive_lock(home / "runtime" / "daemon-control.lock"):
        with _client(home, url, client_factory) as client:
            existing = _status(client)
            record = _read_metadata(home)
            if existing is not None:
                if record and _matches(record, existing) and owner_is_locked(home):
                    return {"state": "running", "already_running": True, **existing}
                raise DaemonError("A service already answers here but is not this recorded instance.")
            if owner_is_locked(home) or (record and _pid_alive(record["pid"])):
                raise DaemonError("An owner/PID is still active; refusing to start another instance.")
            # Check absence under the shared lock; the child acquires it itself.
            with exclusive_lock(owner_lock_path(home)):
                pass
            private_directory(home / "logs")
            log_path = home / "logs" / "daemon.log"
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            env = os.environ.copy()
            env["HARNESS_HOME"] = str(home)
            with os.fdopen(fd, "ab", buffering=0) as log:
                process = popen([sys.executable, "-m", "harness", "--home", str(home),
                                 "serve", "foreground"], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, env=env, start_new_session=True,
                                close_fds=True)
            record = {"home": str(home), "pid": process.pid, "instance_id": None,
                      "started_at": time.time()}
            _write_metadata(home, record)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _metadata_path(home).unlink(missing_ok=True)
                    raise DaemonError(f"Daemon exited before readiness; inspect {log_path}.")
                live = _status(client)
                if live is not None:
                    if live.get("pid") != process.pid or not live.get("instance_id"):
                        raise DaemonError("Readiness belongs to another process; no process was killed.")
                    if live.get("ready") and owner_is_locked(home):
                        record["instance_id"] = live["instance_id"]
                        _write_metadata(home, record)
                        return {"state": "running", "already_running": False, **live}
                time.sleep(0.1)
            raise DaemonError(f"Readiness timed out; record retained, no PID killed. Inspect {log_path}.")


def stop(home: Path, *, url: str | None = None, wait: float = 15.0,
         client_factory: Callable[..., httpx.Client] = httpx.Client) -> dict[str, Any]:
    with exclusive_lock(home / "runtime" / "daemon-control.lock"):
        record = _read_metadata(home)
        if not record:
            raise DaemonError("No owned instance record; refusing to stop an arbitrary service.")
        with _client(home, url, client_factory) as client:
            live = _status(client)
            if live is None:
                if not owner_is_locked(home) and not _pid_alive(record["pid"]):
                    _metadata_path(home).unlink(missing_ok=True)
                    return {"state": "stopped", "already_stopped": True}
                raise DaemonError("Recorded owner is unreachable; no PID will be killed.")
            if not _matches(record, live) or not owner_is_locked(home):
                raise DaemonError("PID/instance/owner lock mismatch; refusing shutdown.")
            response = client.post("/api/shutdown", headers={
                "X-Harness-Instance-Id": record["instance_id"]})
            if response.status_code >= 400:
                raise DaemonError(f"Shutdown rejected (HTTP {response.status_code}).")
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not owner_is_locked(home) and not _pid_alive(record["pid"]):
                    _metadata_path(home).unlink(missing_ok=True)
                    return {"state": "stopped"}
                time.sleep(0.1)
            return {"state": "draining", "message": "Shutdown requested; ownership record retained."}


def logs(home: Path, *, lines: int = 100) -> str:
    """Read bounded tail of the daemon log without inspecting secret files."""
    if lines < 1 or lines > 10000:
        raise DaemonError("Log lines must be between 1 and 10000.")
    path = home / "logs" / "daemon.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            size = min(end, 1_048_576)
            stream.seek(end - size)
            content = stream.read(size).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return "No daemon log yet."
    return "\n".join(content.splitlines()[-lines:])
