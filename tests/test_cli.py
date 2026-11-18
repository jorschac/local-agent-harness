"""CLI tests use MockTransport only: no sockets, subprocesses, models or browser."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from harness import cli, daemon
from harness.paths import init_home, path_map

runner = CliRunner()


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "state"
    init_home(root)
    return root


@pytest.fixture
def stub(monkeypatch):
    requests = []
    respond = {"handler": lambda request: httpx.Response(200, json={"ok": True})}
    original_client = httpx.Client

    def transport(request):
        requests.append(request)
        return respond["handler"](request)

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(cli, "HTTP_CLIENT_FACTORY", factory)
    return requests, respond, factory


def invoke(home, *args, **kwargs):
    return runner.invoke(cli.app, ["--home", str(home), *args], **kwargs)


def body(request):
    return json.loads(request.content)


def test_help_is_provider_free(monkeypatch):
    monkeypatch.setattr(cli, "HTTP_CLIENT_FACTORY", lambda **kw: pytest.fail("HTTP on help"))
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("serve", "config", "plugin", "tools", "run", "chat", "runs", "session", "credentials"):
        assert name in result.output


def test_init_private_idempotent_and_no_token_output(home, stub):
    token_file = path_map(home)["token"]
    token = token_file.read_text()
    result = invoke(home, "init")
    assert result.exit_code == 0, result.output
    assert token_file.read_text() == token
    assert token.strip() not in result.output
    assert (home.stat().st_mode & 0o777) == 0o700
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert not stub[0]


@pytest.mark.parametrize("args,method,path,expected", [
    (["config", "get"], "GET", "/api/config", None),
    (["config", "set", "model", "openai/gpt-4.1-mini", "--expected-revision", "3"], "PATCH", "/api/config", {"path": "model", "value": "openai/gpt-4.1-mini", "expected_revision": 3}),
    (["config", "unset", "rounds"], "PATCH", "/api/config", {"path": "rounds", "unset": True}),
    (["plugin", "list"], "GET", "/api/plugins/list", None),
    (["plugin", "show", "arithmetic"], "GET", "/api/plugins/arithmetic", None),
    (["plugin", "enable", "arithmetic", "--trust"], "POST", "/api/plugins/arithmetic/enable", {"trust": True}),
    (["plugin", "disable", "arithmetic"], "POST", "/api/plugins/arithmetic/disable", {}),
    (["plugin", "config", "set", "browser-safety", "quota", "3"], "PUT", "/api/plugins/browser-safety/settings", {"key": "quota", "value": 3}),
    (["plugin", "config", "unset", "browser-safety", "quota"], "PUT", "/api/plugins/browser-safety/settings", {"key": "quota", "unset": True}),
    (["runs", "list"], "GET", "/api/runs", None),
    (["runs", "show", "run-1"], "GET", "/api/runs/run-1", None),
    (["runs", "cancel", "run-1"], "POST", "/api/runs/run-1/cancel", {}),
    (["session", "new"], "POST", "/api/sessions", {}),
    (["session", "list"], "GET", "/api/sessions", None),
    (["session", "show", "session-1"], "GET", "/api/sessions/session-1", None),
    (["session", "messages", "session-1"], "GET", "/api/sessions/session-1/messages", None),
    (["session", "close", "session-1"], "POST", "/api/sessions/session-1/close", {}),
])
def test_http_command_routing(home, stub, args, method, path, expected):
    result = invoke(home, *args)
    assert result.exit_code == 0, (result.output, result.exception)
    request = stub[0][-1]
    assert (request.method, request.url.path) == (method, path)
    assert request.headers["Authorization"].startswith("Bearer ")
    if expected is not None:
        assert body(request) == expected


def test_plugin_add_explicit_full_trust(home, stub, tmp_path):
    result = invoke(home, "plugin", "add", str(tmp_path / "p"), "--trust", "--replace")
    assert result.exit_code == 0, result.output
    assert body(stub[0][-1]) == {"path": str(tmp_path / "p"), "trust": True, "replace": True}


def test_plugin_settings_read_from_details(home, stub):
    stub[1]["handler"] = lambda request: httpx.Response(200, json={"settings": {"quota": 3}})
    result = invoke(home, "plugin", "config", "get", "browser-safety", "quota")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "3"


@pytest.mark.parametrize("selection,expected", [([], None), (["--plugin", "arithmetic"], ["arithmetic"]),
    (["--plugin", "arithmetic", "--plugin", "browser-safety"], ["arithmetic", "browser-safety"]),
    (["--no-plugins"], [])])
def test_run_complete_plugin_replacement_never_persists(home, stub, selection, expected):
    config_file = path_map(home)["config"]
    before = config_file.read_bytes()
    stub[1]["handler"] = lambda request: httpx.Response(202, json={"run_id": "r1", "session_id": "s1", "status": "queued"})
    result = invoke(home, "run", "test", "--detach", "--session", "s1", *selection)
    assert result.exit_code == 0, (result.output, result.exception)
    request = stub[0][-1]
    data = body(request)
    if expected is None:
        assert "harness_plugins" not in data
    else:
        assert data["harness_plugins"] == expected
    assert data["model"] == "harness-default"
    assert data["messages"] == [{"role": "user", "content": "test"}]
    assert request.headers["X-Harness-Session-Id"] == "s1"
    assert request.headers["Idempotency-Key"]
    assert config_file.read_bytes() == before
    assert len(stub[0]) == 1


def test_selection_conflict_does_not_send_request(home, stub):
    result = invoke(home, "run", "test", "--plugin", "arithmetic", "--no-plugins")
    assert result.exit_code == 1
    assert not stub[0]


@pytest.mark.parametrize("selection,expected", [([], None), (["--plugin", "arithmetic"], ["arithmetic"]), (["--no-plugins"], [])])
def test_tools_inspection_selection(home, stub, selection, expected):
    result = invoke(home, "tools", "list", *selection)
    assert result.exit_code == 0, result.output
    request = stub[0][-1]
    if expected is None:
        assert "harness_plugins" not in request.url.params
    else:
        assert json.loads(request.url.params["harness_plugins"]) == expected


def test_direct_tool_is_http_only(home, stub):
    stub[1]["handler"] = lambda req: httpx.Response(202, json={"run_id": "r1", "kind": "tool"})
    result = invoke(home, "tools", "call", "arithmetic_add", "--args", '{"a":1,"b":2}', "--plugin", "arithmetic", "--detach")
    assert result.exit_code == 0, result.output
    assert body(stub[0][-1]) == {"name": "arithmetic_add", "arguments": {"a": 1, "b": 2}, "harness_plugins": ["arithmetic"]}
    assert stub[0][-1].url.path == "/api/tools/call"


def test_invalid_tool_arguments_do_not_execute(home, stub):
    result = invoke(home, "tools", "call", "arithmetic_add", "--args", "[]")
    assert result.exit_code == 1
    assert not stub[0]


def test_poll_fact_trace_and_final_result(home, stub):
    statuses = iter([
        {"status": "running", "events": [{"type": "tool_call", "name": "arithmetic_add"}]},
        {"status": "completed", "events": [{"type": "tool_call", "name": "arithmetic_add"}],
         "result": {"choices": [{"message": {"content": "5.85"}}]}},
    ])
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "session_id": "s1", "status": "queued"})
        return httpx.Response(200, json=next(statuses))
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test", "--poll")
    assert result.exit_code == 0, (result.output, result.exception)
    assert "arithmetic_add" in result.output
    assert result.output.count("arithmetic_add") == 1
    assert "5.85" in result.output


def test_sse_fact_trace_even_fast_completion(home, stub):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "status": "queued", "events_url": "https://foreign.test/steal"})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                text='id: 1\nevent: tool_call\ndata: {"name":"arithmetic_parse"}\n\nid: 2\nevent: completed\ndata: {"status":"completed"}\n\n')
        return httpx.Response(200, json={"status": "completed", "result": {"choices": [{"message": {"content": "5.85"}}]}})
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test")
    assert result.exit_code == 0, (result.output, result.exception)
    assert "arithmetic_parse" in result.output and "5.85" in result.output
    assert all(request.url.host == "127.0.0.1" for request in stub[0])


def test_ctrl_c_explicit_cancel_and_noncompletion(home, stub):
    def respond(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelling"})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "status": "queued"})
        raise KeyboardInterrupt
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test", "--poll")
    assert result.exit_code == 130, (result.output, result.exception)
    assert stub[0][-1].url.path == "/api/runs/r1/cancel"
    assert '"cancel_requested": true' in result.output
    assert "not assumed complete" in result.output


def test_chat_reuses_session_header_and_leaves_open(home, stub):
    def respond(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "session_id": "s1"})
        return httpx.Response(200, json={"status": "completed", "result": "hello"})
    stub[1]["handler"] = respond
    result = invoke(home, "chat", "--poll", input="hi\n/exit\n")
    assert result.exit_code == 0, (result.output, result.exception)
    submitted = next(req for req in stub[0] if req.url.path == "/api/runs")
    assert submitted.headers["X-Harness-Session-Id"] == "s1"
    assert not any(req.url.path.endswith("/close") for req in stub[0])


def test_connection_failure_offline_management_under_owner_lock(home, stub, monkeypatch):
    calls = []
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)
    stub[1]["handler"] = unavailable
    fake = SimpleNamespace(dispatch=lambda root, action, payload: calls.append((root, action, payload, daemon.owner_is_locked(root))) or {"rounds": 4})
    monkeypatch.setitem(sys.modules, "harness.management", fake)
    result = invoke(home, "config", "set", "rounds", "4")
    assert result.exit_code == 0, (result.output, result.exception)
    assert calls == [(home, "config.set", {"path": "rounds", "value": 4}, True)]
    assert not daemon.owner_is_locked(home)


def test_offline_management_refuses_locked_owner(home, stub, monkeypatch):
    stub[1]["handler"] = lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline", request=req))
    monkeypatch.setitem(sys.modules, "harness.management", SimpleNamespace(dispatch=lambda *a: pytest.fail("dispatch under live owner")))
    with daemon.exclusive_lock(daemon.owner_lock_path(home)):
        result = invoke(home, "config", "get")
    assert result.exit_code == 1
    assert "lock is held" in result.output


@pytest.mark.parametrize("status", [401, 409, 422, 500])
def test_http_errors_never_fallback_to_offline(home, stub, monkeypatch, status):
    monkeypatch.setattr(daemon, "offline_dispatch", lambda *args: pytest.fail("HTTP error caused offline fallback"))
    stub[1]["handler"] = lambda req: httpx.Response(status, json={"error": {"message": "rejected", "code": "test_error"}})
    result = invoke(home, "config", "set", "rounds", "4")
    assert result.exit_code == 1 and "rejected [test_error]" in result.output


def test_read_timeout_does_not_retry_mutating_dispatch(home, stub, monkeypatch):
    stub[1]["handler"] = lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("uncertain", request=req))
    monkeypatch.setattr(daemon, "offline_dispatch", lambda *args: pytest.fail("ambiguous mutation retry"))
    result = invoke(home, "config", "set", "rounds", "4")
    assert result.exit_code == 1


def test_execution_never_falls_back_offline(home, stub, monkeypatch):
    stub[1]["handler"] = lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline", request=req))
    monkeypatch.setattr(daemon, "offline_dispatch", lambda *args: pytest.fail("offline model/tool execution"))
    result = invoke(home, "run", "test", "--detach")
    assert result.exit_code == 1 and "no offline fallback" in " ".join(result.output.split())


def test_credentials_status_does_not_read_api_key(home, stub, monkeypatch):
    key_path = home / "secrets" / "openrouter.key"
    key_path.write_text("synthetic-secret-never-read")
    original_read = Path.read_text
    def guarded(path, *args, **kwargs):
        if path == key_path:
            pytest.fail("API key read")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    result = invoke(home, "credentials", "status")
    assert result.exit_code == 0, result.output
    assert "synthetic-secret" not in result.output
    assert '"openrouter_key_file_present": true' in result.output
    assert not stub[0]


@pytest.mark.parametrize("url", ["https://127.0.0.1:8765", "http://example.com", "http://user:pass@localhost", "http://localhost/api", "http://localhost?x=1"])
def test_remote_or_credentialed_url_refused(home, stub, url):
    result = runner.invoke(cli.app, ["--home", str(home), "--url", url, "config", "get"])
    assert result.exit_code == 1 and not stub[0]


def test_home_environment_override(home, stub):
    result = runner.invoke(cli.app, ["config", "get"], env={"HARNESS_HOME": str(home)})
    assert result.exit_code == 0, result.output
    assert len(stub[0]) == 1


def test_unsafe_path_id_rejected(home, stub):
    result = invoke(home, "plugin", "show", "../secret")
    assert result.exit_code == 1 and not stub[0]


def metadata(home, pid=4242, instance="own-token"):
    value = {"home": str(home.resolve()), "pid": pid, "instance_id": instance}
    daemon._write_metadata(home, value)
    return value


@pytest.mark.parametrize("live", [{"pid": 4242, "instance_id": "foreign-token"},
                                    {"pid": 9999, "instance_id": "own-token"}])
def test_stop_refuses_reused_pid_or_wrong_instance(home, stub, monkeypatch, live):
    metadata(home)
    stub[1]["handler"] = lambda req: httpx.Response(200, json=live)
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: True)
    monkeypatch.setattr(daemon.os, "kill", lambda *args: pytest.fail("signal sent to PID"))
    with pytest.raises(daemon.DaemonError, match="mismatch"):
        daemon.stop(home, client_factory=stub[2], wait=0)
    assert [req.method for req in stub[0]] == ["GET"]


def test_stop_refuses_missing_metadata_no_network(home, stub):
    with pytest.raises(daemon.DaemonError, match="No owned instance"):
        daemon.stop(home, client_factory=stub[2], wait=0)
    assert not stub[0]


def test_stop_verified_requests_shutdown_without_termination_signal(home, stub, monkeypatch):
    metadata(home)
    states = iter([True, False])
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: next(states))
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(daemon.os, "kill", lambda *args: pytest.fail("termination signal"))
    stub[1]["handler"] = lambda req: httpx.Response(200, json={"pid": 4242, "instance_id": "own-token", "ready": True})
    result = daemon.stop(home, client_factory=stub[2], wait=1)
    assert result["state"] == "stopped"
    assert stub[0][-1].url.path == "/api/shutdown"
    assert stub[0][-1].headers["X-Harness-Instance-Id"] == "own-token"
    assert not (home / "runtime" / "daemon.json").exists()


def test_unreachable_live_owner_never_killed(home, stub, monkeypatch):
    metadata(home)
    stub[1]["handler"] = lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline", request=req))
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: True)
    monkeypatch.setattr(daemon.os, "kill", lambda *args: pytest.fail("signal to unreachable PID"))
    with pytest.raises(daemon.DaemonError, match="unreachable"):
        daemon.stop(home, client_factory=stub[2])


def test_detached_start_stub_only_writes_verified_record(home, stub, monkeypatch):
    calls = []
    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=4242, poll=lambda: None)
    statuses = iter([None, {"pid": 4242, "instance_id": "fresh-token", "ready": True}])
    def respond(req):
        value = next(statuses)
        if value is None:
            raise httpx.ConnectError("offline", request=req)
        return httpx.Response(200, json=value)
    stub[1]["handler"] = respond
    locks = iter([False, True])
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: next(locks))
    result = daemon.start(home, client_factory=stub[2], popen=fake_popen)
    assert result["state"] == "running"
    assert daemon._read_metadata(home)["instance_id"] == "fresh-token"
    command, kwargs = calls[0]
    assert command[-2:] == ["serve", "foreground"]
    assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
    assert kwargs["env"]["HARNESS_HOME"] == str(home)
    assert kwargs["stdin"] == daemon.subprocess.DEVNULL
    assert ((home / "runtime" / "daemon.json").stat().st_mode & 0o777) == 0o600


def test_start_refuses_unmanaged_service(home, stub):
    stub[1]["handler"] = lambda req: httpx.Response(200, json={"pid": 4242, "instance_id": "foreign-token", "ready": True})
    with pytest.raises(daemon.DaemonError, match="not this recorded instance"):
        daemon.start(home, client_factory=stub[2], popen=lambda *a, **k: pytest.fail("spawned duplicate service"))


def test_status_distinguishes_unmanaged_and_verified(home, stub, monkeypatch):
    stub[1]["handler"] = lambda req: httpx.Response(200, json={"pid": 4242, "instance_id": "own-token", "ready": True})
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: True)
    assert daemon.status(home, client_factory=stub[2])["state"] == "unmanaged"
    metadata(home)
    assert daemon.status(home, client_factory=stub[2])["verified"] is True


def test_foreground_delegates_to_server_main_only(home, monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, "harness.server", SimpleNamespace(main=lambda: called.append(True)))
    result = invoke(home, "serve", "foreground")
    assert result.exit_code == 0, (result.output, result.exception)
    assert called == [True]


def test_logs_local_tail_not_launchagent(home, stub):
    daemon.private_directory(home / "logs")
    (home / "logs" / "daemon.log").write_text("one\ntwo\nthree\n")
    result = invoke(home, "serve", "logs", "--lines", "2")
    assert result.exit_code == 0 and result.output.strip() == "two\nthree"
    assert not stub[0]


@pytest.mark.parametrize("status", ["timed_out", "interrupted", "failed", "cancelled"])
def test_terminal_failure_never_polls_forever(home, stub, status):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1"})
        return httpx.Response(200, json={"status": status, "error": {"error": {"message": "terminal failure", "code": status}}})
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test", "--poll")
    assert result.exit_code == 1
    assert "terminal failure" in result.output
    assert len(stub[0]) == 2


def test_sse_and_snapshot_share_fact_event_identity(home, stub):
    snapshots = iter([
        {"status": "running", "events": [{"id": 1, "type": "tool.call", "data": {"name": "arithmetic_parse"}}]},
        {"status": "completed", "events": [{"id": 1, "type": "tool.call", "data": {"name": "arithmetic_parse"}},
            {"id": 2, "type": "run.completed", "data": {"status": "completed"}}], "result": "5.85"},
    ])
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1"})
        if request.url.path.endswith("/events"):
            assert request.headers["Last-Event-ID"] == "1"
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                text='id: 1\nevent: tool.call\ndata: {"name":"arithmetic_parse"}\n\nid: 2\nevent: run.completed\ndata: {"status":"completed"}\n\n')
        return httpx.Response(200, json=next(snapshots))
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test")
    assert result.exit_code == 0, (result.output, result.exception)
    assert result.output.count("arithmetic_parse") == 1
    assert "5.85" in result.output


def test_tool_final_result_only_not_full_input_snapshot(home, stub):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "r1", "kind": "tool"})
        return httpx.Response(200, json={"status": "completed", "input": {"sentinel": "input-not-final"},
            "result": {"kind": "tool", "name": "arithmetic_add", "result": {"value": 3}}})
    stub[1]["handler"] = respond
    result = invoke(home, "tools", "call", "arithmetic_add", "--args", '{"a":1,"b":2}', "--poll")
    assert result.exit_code == 0, result.output
    assert '"value": 3' in result.output
    assert "input-not-final" not in result.output
    assert stub[0][0].headers["Idempotency-Key"]


def test_ctrl_c_during_acceptance_looks_up_key_then_cancels_without_repost(home, stub):
    accepted = {}
    def respond(request):
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelling"})
        if request.method == "POST":
            accepted["key"] = request.headers["Idempotency-Key"]
            raise KeyboardInterrupt
        return httpx.Response(200, json=[{"run_id": "r1", "idempotency_key": accepted["key"]}])
    stub[1]["handler"] = respond
    result = invoke(home, "run", "test", "--poll")
    assert result.exit_code == 130, (result.output, result.exception)
    assert [(req.method, req.url.path) for req in stub[0]] == [
        ("POST", "/api/runs"), ("GET", "/api/runs"), ("POST", "/api/runs/r1/cancel")]
    assert '"cancel_requested": true' in result.output


def test_foreground_owner_descriptor_is_verified_for_status(home, stub, monkeypatch):
    record = {"home": str(home.resolve()), "pid": 4242, "instance_id": "foreground-token"}
    path_map(home)["owner_descriptor"].write_text(json.dumps(record))
    stub[1]["handler"] = lambda req: httpx.Response(200, json={"pid": 4242, "instance_id": "foreground-token", "ready": True})
    monkeypatch.setattr(daemon, "owner_is_locked", lambda root: True)
    assert daemon.status(home, client_factory=stub[2])["verified"] is True


def test_timed_out_start_record_can_complete_only_from_same_pid_owner(home):
    metadata(home, instance=None)
    path_map(home)["owner_descriptor"].write_text(json.dumps({"home": str(home.resolve()), "pid": 9999, "instance_id": "wrong-process"}))
    assert daemon._read_metadata(home)["instance_id"] is None
    path_map(home)["owner_descriptor"].write_text(json.dumps({"home": str(home.resolve()), "pid": 4242, "instance_id": "own-child"}))
    assert daemon._read_metadata(home)["instance_id"] == "own-child"
