"""Typer frontend for the local owner service.

Model and tool execution is HTTP-only. Pure offline management is serialized
with the server's flock and never constructs a provider or loads a plugin.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse
import uuid

import httpx
import typer
from rich.console import Console

from . import daemon

app = typer.Typer(help="Local agent harness. Plugins have full local trust, not a sandbox.",
                  no_args_is_help=True)
serve_app = typer.Typer(help="Foreground or detached local owner; never installs a LaunchAgent.",
                        invoke_without_command=True)
credentials_app = typer.Typer(help="Credential presence and future masked key entry (never reveals keys).")
config_app = typer.Typer(help="Read or change persistent configuration.")
plugin_app = typer.Typer(help="Manage full-trust standalone plugins.")
plugin_config_app = typer.Typer(help="Plugin settings; changes take effect on the next run.")
tools_app = typer.Typer(help="List tools or execute a direct tool run over HTTP.")
runs_app = typer.Typer(help="Inspect, follow, or explicitly cancel runs.")
session_app = typer.Typer(help="Persistent sessions and message history.")
for name, subapp in (("serve", serve_app), ("credentials", credentials_app),
                     ("config", config_app), ("plugin", plugin_app), ("tools", tools_app),
                     ("runs", runs_app), ("session", session_app)):
    app.add_typer(subapp, name=name)
plugin_app.add_typer(plugin_config_app, name="config")
app.add_typer(serve_app, name="daemon")
console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)
HTTP_CLIENT_FACTORY: Callable[..., httpx.Client] = httpx.Client
TERMINAL = {"completed", "failed", "cancelled", "canceled", "timed_out", "interrupted", "succeeded", "done"}


class CLIError(RuntimeError):
    pass


def handled(function: Callable) -> Callable:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except (CLIError, daemon.DaemonError, httpx.HTTPError, ValueError, OSError) as exc:
            error_console.print(f"Error: {exc}", style="red", markup=False)
            raise typer.Exit(1) from exc
        except Exception as exc:
            # Core domain errors are imported lazily to keep the CLI provider-free.
            from .errors import HarnessError
            if not isinstance(exc, HarnessError):
                raise
            error_console.print(f"Error: {exc}", style="red", markup=False)
            raise typer.Exit(1) from exc
    return wrapper


def emit(value: Any) -> None:
    if isinstance(value, str):
        console.print(value, markup=False)
    else:
        console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def json_value(value: str) -> Any:
    """JSON scalars/objects/arrays, or a plain string for friendly CLI values."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def json_object(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Arguments must be valid JSON: {exc.msg}.") from exc
    if not isinstance(result, dict):
        raise CLIError("Arguments must be a JSON object.")
    return result


def plugin_selection(plugins: Optional[list[str]], no_plugins: bool) -> Optional[list[str]]:
    if plugins and no_plugins:
        raise CLIError("Use either --plugin (complete replacement) or --no-plugins, not both.")
    return [] if no_plugins else (list(plugins) if plugins is not None else None)


def _lookup(value: Any, path: Optional[str]) -> Any:
    if not path:
        return value
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise CLIError(f"Unknown path: {path}")
        value = value[part]
    return value


@dataclass
class State:
    home: Path
    url: Optional[str] = None
    _client: Optional[httpx.Client] = field(default=None, repr=False)

    def client(self) -> httpx.Client:
        if self._client is None:
            token = daemon.read_local_token(self.home)
            self._client = HTTP_CLIENT_FACTORY(
                base_url=self.url or daemon.server_url(self.home),
                headers={"Authorization": f"Bearer {token}"}, timeout=15.0, trust_env=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def request(self, method: str, path: str, *, offline: Optional[str] = None,
                payload: Optional[dict[str, Any]] = None,
                dispatch_payload: Optional[dict[str, Any]] = None,
                headers: Optional[dict[str, str]] = None,
                params: Optional[dict[str, str]] = None) -> Any:
        kwargs: dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        if headers:
            kwargs["headers"] = headers
        if params:
            kwargs["params"] = params
        try:
            response = self.client().request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if offline is None:
                raise CLIError("Local service unavailable. Run 'harness serve start'; execution has no offline fallback.")
            return daemon.offline_dispatch(self.home, offline,
                                           dispatch_payload if dispatch_payload is not None else (payload or {}))
        if response.status_code >= 400:
            try:
                err = response.json().get("error", {})
            except (ValueError, AttributeError):
                err = {}
            message = err.get("message", response.reason_phrase)
            code = err.get("code", response.status_code)
            raise CLIError(f"{message} [{code}]")
        return response.json() if response.content else {}


@app.callback()
@handled
def main(ctx: typer.Context,
         home: Optional[Path] = typer.Option(None, "--home", envvar="HARNESS_HOME", help="Private state directory."),
         url: Optional[str] = typer.Option(None, "--url", envvar="HARNESS_URL", help="Loopback HTTP service URL.")) -> None:
    from .paths import home_path
    root = home.expanduser().resolve() if home is not None else home_path()
    if url:
        parsed = urlparse(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise CLIError("--url must be a plain loopback HTTP origin without credentials.")
    os.environ["HARNESS_HOME"] = str(root)
    ctx.obj = State(root, url.rstrip("/") if url else None)
    ctx.call_on_close(ctx.obj.close)


def state(ctx: typer.Context) -> State:
    return ctx.obj


@app.command("init")
@handled
def initialize(ctx: typer.Context) -> None:
    from .paths import init_home
    s = state(ctx)
    try:
        with daemon.exclusive_lock(daemon.owner_lock_path(s.home)):
            result = init_home(s.home)
    except daemon.DaemonError:
        # Never initialize/mutate underneath a running owner.
        result = {"home": str(s.home), "already_owned": True,
                  "status": s.request("GET", "/api/status")}
    emit({"initialized": True, "home": str(s.home), "paths": result})


@serve_app.callback()
@handled
def serve(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        daemon.foreground(state(ctx).home)


@serve_app.command("foreground")
@handled
def serve_foreground(ctx: typer.Context) -> None:
    daemon.foreground(state(ctx).home)


@serve_app.command("start")
@handled
def serve_start(ctx: typer.Context, wait: float = typer.Option(15.0, min=0.1, max=300.0)) -> None:
    s = state(ctx)
    emit(daemon.start(s.home, url=s.url, wait=wait, client_factory=HTTP_CLIENT_FACTORY))


@serve_app.command("stop")
@handled
def serve_stop(ctx: typer.Context, wait: float = typer.Option(15.0, min=0.1, max=300.0)) -> None:
    s = state(ctx)
    emit(daemon.stop(s.home, url=s.url, wait=wait, client_factory=HTTP_CLIENT_FACTORY))


@serve_app.command("status")
@handled
def serve_status(ctx: typer.Context) -> None:
    s = state(ctx)
    emit(daemon.status(s.home, url=s.url, client_factory=HTTP_CLIENT_FACTORY))


@serve_app.command("logs")
@handled
def serve_logs(ctx: typer.Context, lines: int = typer.Option(100, "--lines", "-n", min=1, max=10000)) -> None:
    emit(daemon.logs(state(ctx).home, lines=lines))


@credentials_app.command("status")
@handled
def credentials_status(ctx: typer.Context) -> None:
    from .paths import path_map
    s = state(ctx)
    emit({"local_token_present": daemon.token_path(s.home).is_file(),
          "openrouter_key_file_present": path_map(s.home)["key"].is_file(),
          "openrouter_environment_present": "OPENROUTER_API_KEY" in os.environ,
          "values": "never read or displayed"})


@credentials_app.command("set")
@handled
def credentials_set(ctx: typer.Context) -> None:
    """Future explicit masked interaction. Does not execute any model request."""
    from .paths import path_map
    s = state(ctx)
    value = typer.prompt("New OpenRouter key (hidden)", hide_input=True, confirmation_prompt=True)
    if not value.strip() or "\n" in value or "\r" in value:
        raise CLIError("Key must be nonempty and on one line.")
    with daemon.exclusive_lock(daemon.owner_lock_path(s.home)):
        path = path_map(s.home)["key"]
        daemon.private_directory(path.parent)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value.strip() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    emit("Credential saved privately. No key displayed and no model called; restart the owner to reload.")


@config_app.command("get")
@handled
def config_get(ctx: typer.Context, path: Optional[str] = typer.Argument(None)) -> None:
    value = state(ctx).request("GET", "/api/config", offline="config.get")
    emit(_lookup(value, path))


@config_app.command("set")
@handled
def config_set(ctx: typer.Context, path: str, value: str,
               expected_revision: Optional[int] = typer.Option(None, "--expected-revision")) -> None:
    payload: dict[str, Any] = {"path": path, "value": json_value(value)}
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    emit(state(ctx).request("PATCH", "/api/config", payload=payload, offline="config.set"))


@config_app.command("unset")
@handled
def config_unset(ctx: typer.Context, path: str,
                 expected_revision: Optional[int] = typer.Option(None, "--expected-revision")) -> None:
    payload: dict[str, Any] = {"path": path, "unset": True}
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    emit(state(ctx).request("PATCH", "/api/config", payload=payload, offline="config.unset"))


@plugin_app.command("add")
@handled
def plugin_add(ctx: typer.Context, path: Path,
               trust: bool = typer.Option(False, "--trust", help="Acknowledge arbitrary full-trust local plugin code."),
               replace: bool = typer.Option(False, "--replace")) -> None:
    payload = {"path": str(path.expanduser().resolve()), "trust": trust, "replace": replace}
    emit(state(ctx).request("POST", "/api/plugins", payload=payload, offline="plugins.add"))


@plugin_app.command("list")
@handled
def plugin_list(ctx: typer.Context) -> None:
    emit(state(ctx).request("GET", "/api/plugins/list", offline="plugins.list"))


def safe_id(value: str) -> str:
    # IDs are single path segments; reject traversal and query/fragment tricks.
    if not value or value in {".", ".."} or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        raise CLIError("ID must contain only letters, digits, hyphens or underscores.")
    return value


@plugin_app.command("show")
@handled
def plugin_show(ctx: typer.Context, plugin_id: str) -> None:
    plugin_id = safe_id(plugin_id)
    emit(state(ctx).request("GET", f"/api/plugins/{plugin_id}", offline="plugins.show",
                            dispatch_payload={"id": plugin_id}))


@plugin_app.command("enable")
@handled
def plugin_enable(ctx: typer.Context, plugin_id: str,
                  trust: bool = typer.Option(False, "--trust", help="Acknowledge full local execution trust.")) -> None:
    plugin_id = safe_id(plugin_id)
    emit(state(ctx).request("POST", f"/api/plugins/{plugin_id}/enable", payload={"trust": trust},
                            offline="plugins.enable", dispatch_payload={"id": plugin_id, "trust": trust}))


@plugin_app.command("disable")
@handled
def plugin_disable(ctx: typer.Context, plugin_id: str) -> None:
    plugin_id = safe_id(plugin_id)
    emit(state(ctx).request("POST", f"/api/plugins/{plugin_id}/disable", payload={},
                            offline="plugins.disable", dispatch_payload={"id": plugin_id}))


@plugin_config_app.command("get")
@handled
def plugin_config_get(ctx: typer.Context, plugin_id: str, key: Optional[str] = typer.Argument(None)) -> None:
    plugin_id = safe_id(plugin_id)
    # The HTTP contract exposes settings through plugin details, not a GET /settings route.
    value = state(ctx).request("GET", f"/api/plugins/{plugin_id}", offline="plugins.show",
                               dispatch_payload={"id": plugin_id})
    settings = value.get("settings", value.get("config", {}))
    emit(_lookup(settings, key))


@plugin_config_app.command("set")
@handled
def plugin_config_set(ctx: typer.Context, plugin_id: str, key: str, value: str) -> None:
    plugin_id = safe_id(plugin_id)
    payload = {"key": key, "value": json_value(value)}
    emit(state(ctx).request("PUT", f"/api/plugins/{plugin_id}/settings", payload=payload,
                            offline="plugins.settings.set", dispatch_payload={"id": plugin_id, **payload}))


@plugin_config_app.command("unset")
@handled
def plugin_config_unset(ctx: typer.Context, plugin_id: str, key: str) -> None:
    plugin_id = safe_id(plugin_id)
    payload = {"key": key, "unset": True}
    emit(state(ctx).request("PUT", f"/api/plugins/{plugin_id}/settings", payload=payload,
                            offline="plugins.settings.unset", dispatch_payload={"id": plugin_id, **payload}))


def _selection_body(plugins: Optional[list[str]], no_plugins: bool) -> dict[str, Any]:
    selected = plugin_selection(plugins, no_plugins)
    return {} if selected is None else {"harness_plugins": selected}


def _model(s: State, model: Optional[str]) -> str:
    if model:
        return model
    # Read persisted configuration, never a provider instance.
    from .paths import load_config
    return str(load_config(s.home).get("alias", "harness-default"))


def _submit_execution(s: State, path: str, payload: dict[str, Any],
                      headers: dict[str, str]) -> dict[str, Any]:
    try:
        return s.request("POST", path, payload=payload, headers=headers)
    except KeyboardInterrupt:
        # Acceptance may have happened even though the response was interrupted.
        # Query the durable key; never replay POST or submit another model run.
        key = headers["Idempotency-Key"]
        try:
            runs = s.request("GET", "/api/runs")
            matched = next((item for item in runs if isinstance(item, dict)
                            and item.get("idempotency_key") == key), None)
            if matched:
                run_id = safe_id(str(matched.get("run_id", matched.get("id", ""))))
                result = s.request("POST", f"/api/runs/{run_id}/cancel", payload={})
                emit({"cancel_requested": True, "run_id": run_id, "service": result,
                      "note": "Cancellation requested, not assumed complete."})
            else:
                emit({"cancel_requested": False, "idempotency_key": key,
                      "note": "Acceptance unconfirmed; no execution was resubmitted. Inspect runs list."})
        except (CLIError, httpx.HTTPError) as exc:
            emit({"cancel_requested": False, "idempotency_key": key, "error": str(exc),
                  "note": "Acceptance unconfirmed; no execution was resubmitted."})
        raise typer.Exit(130)


def _submit_run(s: State, prompt: str, *, model: Optional[str], session: Optional[str],
                plugins: Optional[list[str]], no_plugins: bool, max_tokens: Optional[int]) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": _model(s, model),
                              "messages": [{"role": "user", "content": prompt}],
                              **_selection_body(plugins, no_plugins)}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    if session:
        headers["X-Harness-Session-Id"] = safe_id(session)
    return _submit_execution(s, "/api/runs", payload, headers)


def _relative_run_url(value: Optional[str], fallback: str) -> str:
    # Do not send the local bearer token to absolute URLs returned in a payload.
    if value and value.startswith("/api/runs/") and not value.startswith("//") and ":" not in value:
        return value
    return fallback


def _final_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        choices = value.get("choices")
        if choices and isinstance(choices, list):
            message = choices[0].get("message", {})
            if isinstance(message.get("content"), str):
                return message["content"]
        for key in ("output_text", "final_text", "content"):
            if isinstance(value.get(key), str):
                return value[key]
        for key in ("result", "response", "completion", "output", "final"):
            if key in value:
                text = _final_text(value[key])
                if text is not None:
                    return text
    return None


def _show_snapshot_events(snapshot: dict[str, Any], seen: set[str]) -> Optional[str]:
    events = snapshot.get("events", snapshot.get("trace", []))
    if not isinstance(events, list):
        return None
    cursor: Optional[str] = None
    for index, event in enumerate(events):
        event_id = event.get("id") if isinstance(event, dict) else None
        key = f"event:{event_id}" if event_id is not None else f"snapshot:{index}:{json.dumps(event, sort_keys=True, default=str)}"
        if event_id is not None:
            cursor = str(event_id)
        if key not in seen:
            seen.add(key)
            emit({"trace": event})
    return cursor


def _finish(snapshot: dict[str, Any]) -> None:
    text = _final_text(snapshot)
    if text is not None:
        emit(text)
    elif snapshot.get("error"):
        emit(snapshot["error"])
    elif snapshot.get("result") is not None:
        emit(snapshot["result"])
    else:
        emit(snapshot)
    if snapshot.get("status") in {"failed", "cancelled", "canceled", "timed_out", "interrupted"}:
        raise typer.Exit(1)


def _sse_events(s: State, url: str, seen: set[str], last_id: Optional[str]) -> Optional[str]:
    headers = {"Accept": "text/event-stream"}
    if last_id:
        headers["Last-Event-ID"] = last_id
    with s.client().stream("GET", url, headers=headers) as response:
        if response.status_code >= 400:
            raise CLIError(f"Trace request failed (HTTP {response.status_code}).")
        data: list[str] = []
        event = "message"
        event_id: Optional[str] = None
        for line in response.iter_lines():
            if line == "":
                if data:
                    raw = "\n".join(data)
                    if raw == "[DONE]":
                        return last_id
                    try:
                        value: Any = json.loads(raw)
                    except json.JSONDecodeError:
                        value = raw
                    key = f"event:{event_id}" if event_id else f"sse:{event}:{raw}"
                    if key not in seen:
                        seen.add(key)
                        emit({"event": event, "trace": value})
                    if event_id:
                        last_id = event_id
                    # Core streams may leave connection open until loop completion.
                    if event in TERMINAL or (isinstance(value, dict) and value.get("status") in TERMINAL):
                        return last_id
                data, event, event_id = [], "message", None
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("id:"):
                event_id = line[3:].strip()
        return last_id


def follow_run(s: State, run: dict[str, Any], *, poll: bool = False,
               interval: float = 0.25) -> None:
    run_id = safe_id(str(run.get("run_id", run.get("id", ""))))
    status_url = _relative_run_url(run.get("status_url"), f"/api/runs/{run_id}")
    events_url = _relative_run_url(run.get("events_url"), f"/api/runs/{run_id}/events")
    emit({"run_id": run_id, "session_id": run.get("session_id"), "status": run.get("status")})
    seen: set[str] = set()
    last_id: Optional[str] = None
    try:
        while True:
            snapshot = s.request("GET", status_url)
            snapshot_cursor = _show_snapshot_events(snapshot, seen)
            if snapshot_cursor is not None:
                last_id = snapshot_cursor
            if snapshot.get("status") in TERMINAL:
                # A fast completed run still deserves its factual trace.
                if not poll and not snapshot.get("events", snapshot.get("trace")):
                    try:
                        last_id = _sse_events(s, events_url, seen, last_id)
                    except (httpx.ReadTimeout, httpx.RemoteProtocolError):
                        pass
                _finish(snapshot)
                return
            if not poll:
                try:
                    last_id = _sse_events(s, events_url, seen, last_id)
                except (httpx.ReadTimeout, httpx.RemoteProtocolError):
                    pass  # Keep polling owned run; no new model request is submitted.
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        try:
            result = s.request("POST", f"/api/runs/{run_id}/cancel", payload={})
            emit({"cancel_requested": True, "run_id": run_id, "service": result,
                  "note": "Cancellation requested, not assumed complete; inspect runs show."})
        except (CLIError, httpx.HTTPError) as exc:
            emit({"cancel_requested": False, "run_id": run_id, "error": str(exc)})
        raise typer.Exit(130)


@app.command("run")
@handled
def run_command(ctx: typer.Context, prompt: str,
                model: Optional[str] = typer.Option(None),
                session: Optional[str] = typer.Option(None, "--session"),
                plugins: Optional[list[str]] = typer.Option(None, "--plugin", help="Complete run-only plugin replacement; repeat for multiple."),
                no_plugins: bool = typer.Option(False, "--no-plugins", help="Run-only empty plugin selection."),
                max_tokens: Optional[int] = typer.Option(None, "--max-tokens", min=1),
                poll: bool = typer.Option(False, "--poll", help="Poll snapshots instead of SSE."),
                detach: bool = typer.Option(False, "--detach", help="Submit only; inspect with runs commands.")) -> None:
    s = state(ctx)
    created = _submit_run(s, prompt, model=model, session=session, plugins=plugins,
                          no_plugins=no_plugins, max_tokens=max_tokens)
    if detach:
        emit(created)
    else:
        follow_run(s, created, poll=poll)


@app.command("chat")
@handled
def chat_command(ctx: typer.Context, session: Optional[str] = typer.Option(None, "--session"),
                 model: Optional[str] = typer.Option(None),
                 plugins: Optional[list[str]] = typer.Option(None, "--plugin", help="Complete run-only plugin replacement."),
                 no_plugins: bool = typer.Option(False, "--no-plugins"),
                 poll: bool = typer.Option(False, "--poll")) -> None:
    s = state(ctx)
    plugin_selection(plugins, no_plugins)
    if session is None:
        created = s.request("POST", "/api/sessions", payload={})
        session = created.get("session_id", created.get("id"))
        if not session:
            raise CLIError("Session creation did not return an ID.")
    session = safe_id(session)
    emit(f"Session {session}. /exit leaves the session open; session close closes it explicitly.")
    while True:
        try:
            prompt = typer.prompt("You", prompt_suffix="> ")
        except (EOFError, KeyboardInterrupt, typer.Abort):
            return
        if prompt.strip() in {"/exit", "/quit"}:
            return
        if not prompt.strip():
            continue
        created = _submit_run(s, prompt, model=model, session=session, plugins=plugins,
                              no_plugins=no_plugins, max_tokens=None)
        follow_run(s, created, poll=poll)


@runs_app.command("list")
@handled
def runs_list(ctx: typer.Context) -> None:
    emit(state(ctx).request("GET", "/api/runs"))


@runs_app.command("show")
@handled
def runs_show(ctx: typer.Context, run_id: str) -> None:
    emit(state(ctx).request("GET", f"/api/runs/{safe_id(run_id)}"))


@runs_app.command("events")
@handled
def runs_events(ctx: typer.Context, run_id: str, poll: bool = typer.Option(False, "--poll")) -> None:
    follow_run(state(ctx), {"run_id": safe_id(run_id)}, poll=poll)


@runs_app.command("cancel")
@handled
def runs_cancel(ctx: typer.Context, run_id: str) -> None:
    emit(state(ctx).request("POST", f"/api/runs/{safe_id(run_id)}/cancel", payload={}))


@session_app.command("new")
@handled
def session_new(ctx: typer.Context) -> None:
    emit(state(ctx).request("POST", "/api/sessions", payload={}))


@session_app.command("list")
@handled
def session_list(ctx: typer.Context) -> None:
    emit(state(ctx).request("GET", "/api/sessions"))


@session_app.command("show")
@handled
def session_show(ctx: typer.Context, session_id: str) -> None:
    emit(state(ctx).request("GET", f"/api/sessions/{safe_id(session_id)}"))


@session_app.command("messages")
@handled
def session_messages(ctx: typer.Context, session_id: str) -> None:
    emit(state(ctx).request("GET", f"/api/sessions/{safe_id(session_id)}/messages"))


@session_app.command("close")
@handled
def session_close(ctx: typer.Context, session_id: str) -> None:
    emit(state(ctx).request("POST", f"/api/sessions/{safe_id(session_id)}/close", payload={}))


@tools_app.command("list")
@handled
def tools_list(ctx: typer.Context,
               plugins: Optional[list[str]] = typer.Option(None, "--plugin", help="Complete inspection-only plugin selection."),
               no_plugins: bool = typer.Option(False, "--no-plugins")) -> None:
    selected = plugin_selection(plugins, no_plugins)
    params = {"harness_plugins": json.dumps(selected)} if selected is not None else None
    emit(state(ctx).request("GET", "/api/tools", params=params, offline="tools.list",
                            dispatch_payload={"plugins": selected}))


@tools_app.command("call")
@handled
def tools_call(ctx: typer.Context, name: str,
               arguments: str = typer.Option("{}", "--arguments", "--args", help="Strict JSON object."),
               plugins: Optional[list[str]] = typer.Option(None, "--plugin", help="Complete tool-run-only plugin replacement."),
               no_plugins: bool = typer.Option(False, "--no-plugins"),
               poll: bool = typer.Option(False, "--poll"),
               detach: bool = typer.Option(False, "--detach")) -> None:
    payload = {"name": name, "arguments": json_object(arguments), **_selection_body(plugins, no_plugins)}
    s = state(ctx)
    result = _submit_execution(s, "/api/tools/call", payload,
                               {"Idempotency-Key": str(uuid.uuid4())})
    if detach:
        emit(result)
    else:
        follow_run(s, result, poll=poll)
