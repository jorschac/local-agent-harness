"""Standalone headed-Chrome plugin. No host imports, browser sandbox claims, or math tool.

Only explicit Target.closeTarget(success=True) reclaims ownership/quota. The
ledger intentionally favors a leaked reservation over accidentally owning or
closing a foreign page after an uncertain create/close response.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EXTRACT_SCRIPT = """(() => {
  const text = document.body ? document.body.innerText : '';
  return {title: document.title, url: location.href,
          text: text.slice(0, 8000), text_length: text.length};
})()"""
BOUNDARY = (
    "Page JavaScript is not a permissions sandbox: it may modify pages, make "
    "network requests, navigate or open popups. Recognizable owned openerId "
    "descendants consume quota (even above its limit); opener-less/noopener "
    "pages cannot safely be attributed and are never auto-claimed or closed. "
    "Browser eval must not substitute for the arithmetic tools."
)


class BrowserFault(Exception):
    def __init__(self, code, message, execution="not_started"):
        super().__init__(message)
        self.code, self.execution = code, execution

    def result(self):
        return {"error": {"code": self.code, "message": str(self),
                          "execution": self.execution}}


def _private_directory(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrowserFault("state_corrupt", f"Cannot safely read {path.name}: {exc}") from exc


def _atomic_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _file_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _loopback_endpoint(endpoint):
    parsed = urlparse(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.port or not parsed.path.startswith("/devtools/browser/")):
        raise BrowserFault("unsafe_endpoint", "Refusing non-owned/non-loopback CDP endpoint")
    return endpoint


class ChromeRuntime:
    """Own one headed Chrome, never adopt a user's arbitrary running CDP instance.

    Runtime descriptor is saved *before* spawning. An uncertain spawn is never
    retried automatically. A healthy process is reused across plugin instances.
    """
    def __init__(self, state_dir, executable=DEFAULT_CHROME, startup_timeout=15,
                 *, launcher=None, probe=None, sleeper=None, clock=None):
        self.state_dir = _private_directory(state_dir)
        self.profile = self.state_dir / "chrome-profile"
        self.descriptor = self.state_dir / "runtime.json"
        self.executable = str(executable)
        self.startup_timeout = float(startup_timeout)
        self.launcher = launcher or subprocess.Popen
        self.probe = probe or self._process_status
        self.sleeper = sleeper or time.sleep
        self.clock = clock or time.monotonic

    def _process_status(self, record):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return "uncertain"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "uncertain"
        command = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=3, check=False,
        )
        text = command.stdout.strip()
        expected = [self.executable, f"--user-data-dir={self.profile}",
                    "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                    f"--harness-browser-instance={record['instance_id']}"]
        return "owned" if command.returncode == 0 and all(x in text for x in expected) else "uncertain"

    def _endpoint_from_profile(self):
        active_port = self.profile / "DevToolsActivePort"
        try:
            lines = active_port.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        if len(lines) < 2 or not lines[0].isdigit():
            return None
        endpoint = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        return _loopback_endpoint(endpoint)

    def _wait_endpoint(self, record):
        deadline = self.clock() + self.startup_timeout
        while self.clock() < deadline:
            status = self.probe(record)
            if status == "dead":
                record["status"] = "failed"
                _atomic_json(self.descriptor, record)
                raise BrowserFault("browser_start_failed", "Owned Chrome exited during startup")
            if status != "owned":
                raise BrowserFault("runtime_uncertain", "Cannot verify ownership of the saved Chrome process")
            endpoint = self._endpoint_from_profile()
            if endpoint:
                record.update(status="running", endpoint=endpoint)
                _atomic_json(self.descriptor, record)
                return record
            self.sleeper(0.1)
        raise BrowserFault("browser_start_timeout", "Chrome startup unconfirmed; descriptor retained, no duplicate launch", "uncertain")

    def ensure(self):
        with _file_lock(self.state_dir / "runtime.lock"):
            record = _read_json(self.descriptor, None)
            if record:
                if (record.get("profile") != str(self.profile)
                        or record.get("executable") != self.executable
                        or not isinstance(record.get("instance_id"), str)):
                    raise BrowserFault("runtime_mismatch", "Saved runtime does not match this private Chrome configuration")
                status = self.probe(record)
                if status == "owned":
                    if record.get("endpoint"):
                        _loopback_endpoint(record["endpoint"])
                        # Recheck the private Chrome-generated endpoint, not just a descriptor.
                        if self._endpoint_from_profile() != record["endpoint"]:
                            raise BrowserFault("runtime_uncertain", "Saved CDP endpoint no longer matches owned profile")
                        return record
                    return self._wait_endpoint(record)
                if status != "dead":
                    raise BrowserFault("runtime_uncertain", "Prior spawn/process ownership uncertain; refusing a second Chrome", "uncertain")
                # Chrome descendants may outlive their original PID. Do not bypass profile locks.
                if (self.profile / "SingletonLock").exists() or (self.profile / "SingletonLock").is_symlink():
                    raise BrowserFault("profile_busy", "Owned profile still locked; refusing duplicate Chrome")
            elif ((self.profile / "SingletonLock").exists()
                  or (self.profile / "SingletonLock").is_symlink()
                  or (self.profile / "DevToolsActivePort").exists()):
                raise BrowserFault("runtime_uncertain", "Private profile has runtime traces but no descriptor; refusing adoption or duplicate launch")
            if not Path(self.executable).is_file():
                raise BrowserFault("chrome_unavailable", "Configured headed Chrome executable was not found")
            _private_directory(self.profile)
            active_port = self.profile / "DevToolsActivePort"
            if active_port.exists():
                active_port.unlink()
            record = {"version": 1, "instance_id": uuid.uuid4().hex,
                      "executable": self.executable, "profile": str(self.profile),
                      "status": "starting", "pid": None, "created_at": time.time()}
            _atomic_json(self.descriptor, record)
            command = [self.executable, f"--user-data-dir={self.profile}",
                       "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                       f"--harness-browser-instance={record['instance_id']}",
                       "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank"]
            log_fd = os.open(self.state_dir / "chrome.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.fchmod(log_fd, 0o600)
            try:
                process = self.launcher(command, stdin=subprocess.DEVNULL,
                                        stdout=log_fd, stderr=log_fd, start_new_session=True)
                record["pid"] = process.pid
                _atomic_json(self.descriptor, record)
            except Exception as exc:
                # Popen may have spawned before an error: retain the reservation.
                raise BrowserFault("browser_spawn_uncertain", f"Chrome launch unconfirmed: {exc}", "uncertain") from exc
            finally:
                os.close(log_fd)
            return self._wait_endpoint(record)


class SyncCDPTransport:
    """Synchronous CDP connection for handler threads; every instance is disposable."""
    def __init__(self, endpoint, timeout=20):
        self.endpoint = _loopback_endpoint(endpoint)
        self.timeout = float(timeout)
        self.connection = None
        self.next_id = 0
        self.sessions = {}

    def __enter__(self):
        from websockets.sync.client import connect
        self.connection = connect(self.endpoint, open_timeout=self.timeout,
                                  close_timeout=2, max_size=16 * 1024 * 1024,
                                  proxy=None)
        return self

    def __exit__(self, *args):
        if self.connection is not None:
            self.connection.close()

    def _request(self, method, params, session_id=None):
        self.next_id += 1
        request_id = self.next_id
        request = {"id": request_id, "method": method, "params": params or {}}
        if session_id:
            request["sessionId"] = session_id
        self.connection.send(json.dumps(request, allow_nan=False))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP timeout: {method}")
            response = json.loads(self.connection.recv(timeout=remaining))
            if response.get("id") != request_id:
                continue  # CDP notifications are not successful command acknowledgments.
            if "error" in response:
                error = response["error"]
                raise BrowserFault("cdp_error", f"{method}: {error.get('message', 'CDP error')}", "failed")
            result = response.get("result")
            if not isinstance(result, dict):
                raise BrowserFault("cdp_invalid_response", f"Missing result for {method}", "uncertain")
            return result

    def call(self, method, params=None, target_id=None):
        session_id = None
        if target_id:
            if target_id not in self.sessions:
                attached = self._request("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                self.sessions[target_id] = attached["sessionId"]
            session_id = self.sessions[target_id]
        return self._request(method, params or {}, session_id)


def _check_string(value, label, maximum=2_000_000):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise BrowserFault("invalid_arguments", f"{label} must be a nonempty string of at most {maximum} characters")
    return value


def _check_url(value):
    value = _check_string(value, "url", 32768)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "about"} or (parsed.scheme == "about" and value != "about:blank"):
        raise BrowserFault("invalid_url", "Only http(s) URLs and about:blank are accepted; this is not an SSRF/network sandbox")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise BrowserFault("invalid_url", "http(s) URL must have a host")
    return value


class BrowserService:
    def __init__(self, state_dir, quota=3, *, runtime=None, transport_factory=None,
                 executable=DEFAULT_CHROME, timeout=20, startup_timeout=15):
        if isinstance(quota, bool) or not isinstance(quota, int) or not 1 <= quota <= 100:
            raise BrowserFault("invalid_config", "quota must be an integer from 1 to 100")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0
                   for x in (timeout, startup_timeout)):
            raise BrowserFault("invalid_config", "timeouts must be finite positive numbers")
        self.state_dir = _private_directory(state_dir)
        self.ledger_path = self.state_dir / "ledger.json"
        self.quota = quota
        self.runtime = runtime or ChromeRuntime(self.state_dir, executable, startup_timeout)
        self.transport_factory = transport_factory or (lambda endpoint: SyncCDPTransport(endpoint, timeout))
        self.lock = threading.RLock()

    def _save(self, ledger):
        _atomic_json(self.ledger_path, ledger)

    @contextmanager
    def _transaction(self, session_id):
        _check_string(session_id, "session_id", 512)
        with self.lock, _file_lock(self.state_dir / "ledger.lock"):
            ledger = _read_json(self.ledger_path, {"version": 1, "sessions": {}})
            if ledger.get("version") != 1 or not isinstance(ledger.get("sessions"), dict):
                raise BrowserFault("state_corrupt", "Unsupported browser ledger format")
            session = ledger["sessions"].setdefault(session_id, {"status": "active", "owned": {}, "pending": {}})
            if (session.get("status") not in {"active", "stopping"}
                    or not isinstance(session.get("owned"), dict)
                    or not isinstance(session.get("pending"), dict)):
                raise BrowserFault("state_corrupt", "Invalid session ledger")
            yield ledger, session

    def _active(self, session):
        if session["status"] != "active":
            raise BrowserFault("session_stopping", "Browser session is stopping; only status/close/release may be retried")

    def _usage(self, session):
        return len(session["owned"]) + len(session["pending"])

    def _owned(self, session, target_id, instance_id):
        _check_string(target_id, "target_id", 512)
        entry = session["owned"].get(target_id)
        if not entry:
            raise BrowserFault("foreign_target", "Target is not owned by this session")
        if entry["instance_id"] != instance_id:
            raise BrowserFault("unreclaimed_previous_runtime", "Target belongs to a previous runtime; no confirmed close, quota retained")
        return entry

    def _snapshot(self, ledger, transport, instance_id):
        result = transport.call("Target.getTargets", {})
        targets = result.get("targetInfos")
        if not isinstance(targets, list) or any(not isinstance(t, dict) or not isinstance(t.get("targetId"), str) for t in targets):
            raise BrowserFault("cdp_invalid_response", "Invalid full-browser target snapshot", "failed")
        owners = {}
        ancestry = {}
        for closed in ledger.get("closed_provenance", {}).values():
            if closed["instance_id"] == instance_id:
                ancestry[closed["target_id"]] = closed["owner_session_id"]
        for owner, session in ledger["sessions"].items():
            for target, entry in session["owned"].items():
                if entry["instance_id"] == instance_id:
                    if target in owners:
                        raise BrowserFault("state_corrupt", "Target has multiple owners")
                    owners[target] = owner
                    ancestry[target] = owner
        changed = True
        while changed:
            changed = False
            for info in targets:
                target, opener = info["targetId"], info.get("openerId")
                if info.get("type") != "page" or target in ancestry or opener not in ancestry:
                    continue
                owner = ancestry[opener]
                if target in ledger["sessions"][owner]["owned"]:
                    raise BrowserFault("state_corrupt", "Target ID collision across runtime generations")
                ledger["sessions"][owner]["owned"][target] = {
                    "instance_id": instance_id, "state": "owned", "source": "popup",
                    "opener_id": opener, "created_at": time.time()}
                owners[target] = owner
                ancestry[target] = owner
                changed = True
                self._save(ledger)  # popup discovery survives later command failures
        return targets, owners

    @contextmanager
    def _connection(self):
        record = self.runtime.ensure()
        instance_id = record["instance_id"]
        with self.transport_factory(record["endpoint"]) as transport:
            yield transport, instance_id

    def _guarded(self, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BrowserFault as exc:
            return exc.result()
        except Exception as exc:
            return BrowserFault("transport_error", str(exc), "uncertain").result()

    def open(self, session_id, url="about:blank"):
        return self._guarded(self._open, session_id, url)

    def _open(self, session_id, url):
        url = _check_url(url)
        with self._transaction(session_id) as (ledger, session):
            self._active(session)
            if self._usage(session) >= self.quota:
                raise BrowserFault("quota_exceeded", "Owned/unconfirmed reservations exhaust this session's quota")
            with self._connection() as (transport, instance_id):
                before_targets, _ = self._snapshot(ledger, transport, instance_id)
                preexisting = {target["targetId"] for target in before_targets}
                if self._usage(session) >= self.quota:
                    raise BrowserFault("quota_exceeded", "Owned popup descendants exhaust this session's quota")
                operation_id = uuid.uuid4().hex
                session["pending"][operation_id] = {"instance_id": instance_id, "state": "creating",
                                                    "url": url, "created_at": time.time()}
                self._save(ledger)  # write-ahead reservation before any create request
                try:
                    response = transport.call("Target.createTarget", {"url": url, "newWindow": False})
                    target = response.get("targetId")
                    if not isinstance(target, str) or not target:
                        raise BrowserFault("create_unconfirmed", "CDP returned no targetId", "uncertain")
                    if target in preexisting or any(target in other["owned"] for other in ledger["sessions"].values()):
                        raise BrowserFault("create_unconfirmed", "CDP targetId already existed or was claimed; reservation retained", "uncertain")
                except Exception as exc:
                    session["pending"][operation_id].update(state="uncertain", last_error=str(exc))
                    self._save(ledger)
                    return {"error": {"code": "create_unconfirmed", "message": str(exc), "execution": "uncertain"},
                            "operation_id": operation_id, "quota_used": self._usage(session), "quota": self.quota}
                session["owned"][target] = {"instance_id": instance_id, "state": "owned", "source": "open",
                                            "created_at": time.time()}
                del session["pending"][operation_id]
                self._save(ledger)
                return {"target_id": target, "url": url, "owned": True,
                        "quota_used": self._usage(session), "quota": self.quota,
                        "browser_instance_id": instance_id, "boundary": BOUNDARY}

    def navigate(self, session_id, target_id, url):
        return self._guarded(self._navigate, session_id, target_id, url)

    def _navigate(self, session_id, target_id, url):
        url = _check_url(url)
        with self._transaction(session_id) as (ledger, session):
            self._active(session)
            # Reject foreign IDs without launching/connecting to Chrome.
            if target_id not in session["owned"]:
                raise BrowserFault("foreign_target", "Target is not owned by this session")
            with self._connection() as (transport, instance_id):
                self._snapshot(ledger, transport, instance_id)
                self._owned(session, target_id, instance_id)
                result = transport.call("Page.navigate", {"url": url}, target_id=target_id)
                if result.get("errorText"):
                    raise BrowserFault("navigation_error", result["errorText"], "failed")
                return {"target_id": target_id, "navigation": result, "boundary": BOUNDARY}

    @staticmethod
    def _remote_value(result):
        remote = result.get("result", {})
        if not isinstance(remote, dict):
            raise BrowserFault("cdp_invalid_response", "Invalid Runtime remote result", "failed")
        return {key: remote[key] for key in ("type", "subtype", "value", "unserializableValue", "description") if key in remote}

    def eval(self, session_id, target_id, script):
        return self._guarded(self._eval, session_id, target_id, script)

    def _eval(self, session_id, target_id, script):
        script = _check_string(script, "script")
        with self._transaction(session_id) as (ledger, session):
            self._active(session)
            if target_id not in session["owned"]:
                raise BrowserFault("foreign_target", "Target is not owned by this session")
            with self._connection() as (transport, instance_id):
                self._snapshot(ledger, transport, instance_id)
                self._owned(session, target_id, instance_id)
                transport.call("Runtime.enable", {}, target_id=target_id)
                compiled = transport.call("Runtime.compileScript", {
                    "expression": script, "sourceURL": "harness://browser-eval.js", "persistScript": True,
                }, target_id=target_id)
                if compiled.get("exceptionDetails"):
                    return {"error": {"code": "script_syntax_error", "message": "JavaScript compilation failed", "execution": "not_started"},
                            "target_id": target_id, "exception_details": compiled["exceptionDetails"]}
                script_id = compiled.get("scriptId")
                if not script_id:
                    raise BrowserFault("compile_unconfirmed", "Runtime.compileScript returned no scriptId", "not_started")
                executed = transport.call("Runtime.runScript", {
                    "scriptId": script_id, "returnByValue": True, "awaitPromise": True,
                }, target_id=target_id)
                # Account for recognizable popups even when the script throws.
                self._snapshot(ledger, transport, instance_id)
                if executed.get("exceptionDetails"):
                    return {"error": {"code": "script_runtime_error", "message": "JavaScript execution threw an exception", "execution": "failed"},
                            "target_id": target_id, "exception_details": executed["exceptionDetails"],
                            "result": self._remote_value(executed), "quota_used": self._usage(session)}
                return {"target_id": target_id, "result": self._remote_value(executed),
                        "quota_used": self._usage(session), "boundary": BOUNDARY}

    def extract(self, session_id, target_id, max_chars=4000):
        return self._guarded(self._extract, session_id, target_id, max_chars)

    def _extract(self, session_id, target_id, max_chars):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 8000:
            raise BrowserFault("invalid_arguments", "max_chars must be an integer from 1 to 8000")
        with self._transaction(session_id) as (ledger, session):
            self._active(session)
            if target_id not in session["owned"]:
                raise BrowserFault("foreign_target", "Target is not owned by this session")
            with self._connection() as (transport, instance_id):
                self._snapshot(ledger, transport, instance_id)
                self._owned(session, target_id, instance_id)
                result = transport.call("Runtime.evaluate", {
                    "expression": EXTRACT_SCRIPT, "returnByValue": True, "awaitPromise": False,
                }, target_id=target_id)
                if result.get("exceptionDetails"):
                    return {"error": {"code": "extract_error", "message": "Fixed extraction script failed", "execution": "failed"},
                            "exception_details": result["exceptionDetails"]}
                value = result.get("result", {}).get("value")
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise BrowserFault("extract_error", "Fixed extraction returned unexpected data", "failed")
                return {"target_id": target_id, "title": value.get("title", ""), "url": value.get("url", ""),
                        "text": value["text"][:max_chars], "max_chars": max_chars,
                        "truncated": value.get("text_length", len(value["text"])) > max_chars}

    def _close_one(self, ledger, session, transport, instance_id, target_id):
        entry = self._owned(session, target_id, instance_id)
        entry["state"] = "closing"
        self._save(ledger)
        try:
            result = transport.call("Target.closeTarget", {"targetId": target_id})
            if result.get("success") is not True:
                raise BrowserFault("close_unconfirmed", "CDP did not confirm success: true", "uncertain")
        except Exception as exc:
            entry.update(state="unconfirmed", last_error=str(exc))
            self._save(ledger)
            return {"target_id": target_id, "closed": False, "unconfirmed": True,
                    "error": {"code": "close_unconfirmed", "message": str(exc), "execution": "uncertain"},
                    "quota_used": self._usage(session)}
        ledger.setdefault("closed_provenance", {})[f"{instance_id}:{target_id}"] = {
            "target_id": target_id, "instance_id": instance_id, "owner_session_id": next(
                owner for owner, value in ledger["sessions"].items() if value is session),
            "closed_at": time.time(),
        }
        del session["owned"][target_id]
        self._save(ledger)
        return {"target_id": target_id, "closed": True, "quota_used": self._usage(session)}

    def close(self, session_id, target_id):
        return self._guarded(self._close, session_id, target_id)

    def _close(self, session_id, target_id):
        with self._transaction(session_id) as (ledger, session):
            if target_id not in session["owned"]:
                raise BrowserFault("foreign_target", "Target is not owned by this session")
            try:
                with self._connection() as (transport, instance_id):
                    # Close does not depend on a successful/missing getTargets snapshot.
                    return self._close_one(ledger, session, transport, instance_id, target_id)
            except Exception as exc:
                session["owned"][target_id].update(state="unconfirmed", last_error=str(exc))
                self._save(ledger)
                return {"target_id": target_id, "closed": False, "unconfirmed": True,
                        "error": {"code": "close_unconfirmed", "message": str(exc), "execution": "uncertain"},
                        "quota_used": self._usage(session)}

    def status(self, session_id):
        return self._guarded(self._status, session_id)

    def _status(self, session_id):
        with self._transaction(session_id) as (ledger, session):
            with self._connection() as (transport, instance_id):
                targets, owners = self._snapshot(ledger, transport, instance_id)
                self._save(ledger)
                snapshot = []
                for info in targets:
                    annotated = dict(info)
                    annotated["ownership"] = "self" if owners.get(info["targetId"]) == session_id else ("other_session" if info["targetId"] in owners else "foreign")
                    annotated["owner_session_id"] = owners.get(info["targetId"])
                    snapshot.append(annotated)
                visible = {info["targetId"] for info in targets}
                owned = [{"target_id": target, **entry,
                          "present_in_snapshot": entry["instance_id"] == instance_id and target in visible}
                         for target, entry in session["owned"].items()]
                return {"session_id": session_id, "session_status": session["status"],
                        "browser_instance_id": instance_id, "targets": snapshot, "owned": owned,
                        "pending": [{"operation_id": op, **entry} for op, entry in session["pending"].items()],
                        "quota": self.quota, "quota_used": self._usage(session),
                        "quota_exceeded": self._usage(session) > self.quota, "boundary": BOUNDARY}

    def release(self, session_id):
        return self._guarded(self._release, session_id)

    def _release(self, session_id):
        with self._transaction(session_id) as (ledger, session):
            session["status"] = "stopping"
            self._save(ledger)  # durable stopping before browser I/O or page closes
            closed, failures = [], []
            has_provenance = any(entry["owner_session_id"] == session_id
                                 for entry in ledger.get("closed_provenance", {}).values())
            if session["owned"] or session["pending"] or has_provenance:
                try:
                    with self._connection() as (transport, instance_id):
                        try:
                            self._snapshot(ledger, transport, instance_id)
                        except Exception as exc:
                            failures.append({"phase": "snapshot", "message": str(exc)})
                        for target in list(session["owned"]):
                            try:
                                result = self._close_one(ledger, session, transport, instance_id, target)
                                if result["closed"]:
                                    closed.append(target)
                                else:
                                    failures.append(result)
                            except Exception as exc:
                                session["owned"][target].update(state="unconfirmed", last_error=str(exc))
                                self._save(ledger)
                                failures.append({"target_id": target, "message": str(exc)})
                        try:
                            self._snapshot(ledger, transport, instance_id)
                        except Exception as exc:
                            failures.append({"phase": "final_snapshot", "message": str(exc)})
                except Exception as exc:
                    for entry in session["owned"].values():
                        entry.update(state="unconfirmed", last_error=str(exc))
                    self._save(ledger)
                    failures.append({"phase": "connection", "message": str(exc)})
            unreclaimed = [{"target_id": target, **entry} for target, entry in session["owned"].items()]
            unreclaimed.extend({"operation_id": op, **entry} for op, entry in session["pending"].items())
            return {"session_id": session_id, "session_status": "stopping", "closed": closed,
                    "unreclaimed": unreclaimed, "failures": failures, "quota_used": self._usage(session),
                    "boundary": BOUNDARY}


def _schema(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}


def register(ctx):
    """Hermes-style entry point. Registration never starts Chrome."""
    default_state = Path(os.environ.get("HARNESS_HOME", "~/.harness")).expanduser() / "plugin-data" / "browser-safety"
    service = BrowserService(
        ctx.get_config("state_dir", str(default_state)),
        quota=ctx.get_config("quota", 3), executable=ctx.get_config("chrome_executable", DEFAULT_CHROME),
        timeout=ctx.get_config("timeout", 20), startup_timeout=ctx.get_config("startup_timeout", 15),
    )
    target = {"type": "string", "minLength": 1, "maxLength": 512,
              "description": "target_id returned by browser_open or an owned opener-chain popup; never a foreign target"}
    url = {"type": "string", "minLength": 1, "maxLength": 32768,
           "description": "http(s) URL or about:blank; local/network effects are not sandboxed"}
    tools = [
        ("browser_open", "Open one session-owned page in a singleton headed Chrome using a private profile. Default quota 3; uncertain creates consume quota.",
         {"url": {**url, "default": "about:blank"}}, (), service.open),
        ("browser_navigate", "Navigate an existing owned target without creating a page. Refuses stopping sessions and foreign targets.",
         {"target_id": target, "url": url}, ("target_id", "url"), service.navigate),
        ("browser_eval", "Compile then run JavaScript in an owned page; report syntax/runtime errors. NOT a permissions sandbox; page/network/popup effects possible. Do not use for arithmetic: use arithmetic tools.",
         {"target_id": target, "script": {"type": "string", "minLength": 1, "maxLength": 2_000_000}}, ("target_id", "script"), service.eval),
        ("browser_extract", "Extract title, URL and visible body.innerText using one fixed read script, at most 8000 characters.",
         {"target_id": target, "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000, "default": 4000}}, ("target_id",), service.extract),
        ("browser_close", "Close only this session's owned target. Reclaim quota only if CDP returns success: true; otherwise retain unconfirmed ownership. Allowed while stopping.",
         {"target_id": target}, ("target_id",), service.close),
        ("browser_status", "Full browser target snapshot annotated self/other_session/foreign; persistent owned/pending quota. A missing target never frees quota.",
         {}, (), service.status),
        ("browser_release", "Persist session stopping before cleanup; close only its owned pages, never foreign pages or the browser process. Return unreclaimed reservations; retries allowed.",
         {}, (), service.release),
    ]
    for name, description, properties, required, method in tools:
        def handler(args, _method=method, **kwargs):
            try:
                session_id = kwargs.get("session_id") or "local-direct"
                result = _method(session_id=session_id, **args)
                return json.dumps(result, ensure_ascii=False, allow_nan=False)
            except Exception as exc:
                return json.dumps(BrowserFault("invalid_arguments", str(exc)).result(), ensure_ascii=False)
        ctx.register_tool(name=name, toolset="browser-safety",
                          schema=_schema(name, description, properties, required),
                          handler=handler, is_async=False, description=description)
    return service
