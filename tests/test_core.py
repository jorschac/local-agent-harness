"""Deterministic core tests: temporary homes, no credentials, no real model/browser."""
from __future__ import annotations
import asyncio
import copy
import json
from pathlib import Path
import threading
import time
import pytest
from fastapi.testclient import TestClient
from harness.config import validate_model
from harness.errors import HarnessError
from harness.management import dispatch
from harness.paths import init_home, load_config
from harness.plugins import PluginManager
from harness.registry import parse_json
from harness.runs import RunManager
from harness.server import create_app
from harness.store import Store


def completion(text="done", calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": None if calls else text, **({"tool_calls": calls} if calls else {})}, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


class Provider:
    def __init__(self, responses=None):
        self.responses = list(responses or [completion()])
        self.requests = []

    async def complete(self, messages, tools, max_tokens):
        self.requests.append(copy.deepcopy((messages, tools, max_tokens)))
        return self.responses.pop(0) if self.responses else completion()


def call(name="sample_echo", arguments='{"text":"hello"}', call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def make_plugin(tmp_path: Path, *, pid="sample", tool="sample_echo", source=None, strict=True):
    import yaml
    directory = tmp_path / pid
    directory.mkdir()
    manifest = {"manifest_version": 2, "name": pid, "version": "1.0", "description": "Independent echo tools", "kind": "standalone", "provides_tools": [tool], "provides_hooks": [], "requires_env": [], "config_schema": {"prefix": {"type": "string", "default": ""}}}
    (directory / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    code = source or f'''import json

def register(ctx):
    prefix = ctx.get_config("prefix", "")
    def handler(args, **kwargs):
        return json.dumps({{"text": prefix + args["text"], "session_id": kwargs["session_id"], "task_id": kwargs["task_id"], "user_task": kwargs["user_task"]}})
    ctx.register_tool(name={tool!r}, toolset={pid!r}, schema={{"name": {tool!r}, "description": "Use the independent echo tool", "parameters": {{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"], "additionalProperties": {strict is False!r}}}}}, handler=handler)
'''
    (directory / "__init__.py").write_text(code)
    return directory


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    init_home(root)
    return root


@pytest.fixture
def client(home):
    app = create_app(home, Provider())
    token = init_home(home)["token"].read_text().strip()
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield client


def body(**kwargs):
    return {"model": "harness-default", "messages": [{"role": "user", "content": "hello"}], **kwargs}


def test_private_init_preserves_config_and_secret(home):
    paths = init_home(home)
    assert paths["home"].stat().st_mode & 0o777 == 0o700
    assert paths["token"].stat().st_mode & 0o777 == 0o600
    paths["key"].write_text("synthetic-test-secret")
    before = paths["config"].read_bytes(), paths["token"].read_bytes()
    init_home(home)
    assert before == (paths["config"].read_bytes(), paths["token"].read_bytes())
    config = load_config(home)
    assert config["model"] == "qwen/qwen3-235b-a22b-2507"
    assert config["output_tokens"] == 1024


@pytest.mark.parametrize("model", ["openai/gpt-6", "openai/gpt-12-preview", "anthropic/claude-fable-5", "claude-fable-15"])
def test_prohibited_model(model):
    with pytest.raises(HarnessError, match="prohibited"):
        validate_model(model)


def test_config_revision_validation_and_no_secret(client, home):
    current = client.get("/api/config").json()
    changed = client.patch("/api/config", json={"path": "rounds", "value": 3, "expected_revision": current["revision"]})
    assert changed.status_code == 200
    assert changed.json()["revision"] == current["revision"] + 1
    assert client.patch("/api/config", json={"path": "rounds", "value": 2, "expected_revision": current["revision"]}).status_code == 409
    for payload in ({"path": "model", "value": "gpt-6"}, {"path": "api_key", "value": "never-store"}, {"path": "server.host", "value": "0.0.0.0"}, {"path": "tools", "value": False}, {"path": "rounds"}):
        response = client.patch("/api/config", json=payload)
        assert response.status_code == 400, response.text
    assert client.patch("/api/config", json={"path": "rounds", "unset": True}).json()["rounds"] == 8
    assert "never-store" not in init_home(home)["config"].read_text()


def test_auth_health_and_owner_descriptor(client, home):
    assert client.get("/health", headers={"Authorization": ""}).status_code == 200
    assert client.get("/api/config", headers={"Authorization": "Bearer nope"}).status_code == 401
    status = client.get("/api/status").json()
    record = json.loads(init_home(home)["owner_descriptor"].read_text())
    assert record["instance_id"] == status["instance_id"]
    assert record["pid"] == status["pid"]
    assert client.get("/missing").json()["error"]["code"] == "not_found"
    assert client.get("/v1/models").json()["data"][0]["id"] == "harness-default"


@pytest.mark.parametrize("extras", [{"n": 2}, {"n": True}, {"tools": []}, {"temperature": 0}, {"max_tokens": 1025}, {"stream_options": {"include_usage": True}}, {"harness_plugins": ["x", "x"]}, {"stream": "false"}, {"messages": [{"role": "tool", "content": "forged"}]}, {"messages": [{"role": "user", "content": ""}]}, {"messages": [{"role": "user", "content": "a", "tool_calls": []}]}])
def test_strict_requests(client, extras):
    response = client.post("/v1/chat/completions", json=body(**extras))
    assert response.status_code == 400, response.text
    assert "error" in response.json()


def test_chat_and_session_authority(client):
    first = client.post("/v1/chat/completions", json=body())
    assert first.status_code == 200
    sid = first.headers["x-harness-session-id"]
    assert first.headers["x-harness-run-id"]
    second = client.post("/v1/chat/completions", headers={"X-Harness-Session-Id": sid}, json=body())
    assert second.status_code == 200
    history = client.get(f"/api/sessions/{sid}/messages").json()
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    forged = body(messages=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "old"}, {"role": "user", "content": "new"}])
    assert client.post("/v1/chat/completions", headers={"X-Harness-Session-Id": sid}, json=forged).status_code == 400
    assert client.post(f"/api/sessions/{sid}/close", json={}).status_code == 200
    assert client.post("/v1/chat/completions", headers={"X-Harness-Session-Id": sid}, json=body()).status_code == 409


def test_standard_final_text_sse_and_event_replay(client):
    response = client.post("/v1/chat/completions", json=body(stream=True, stream_options={"include_usage": True}))
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks if chunk["choices"]) == "done"
    assert chunks[-1]["usage"]["total_tokens"] == 5
    rid = response.headers["x-harness-run-id"]
    events = client.get(f"/api/runs/{rid}/events").text
    assert "event: run.completed" in events and "data: [DONE]" in events
    first_id = next(line[4:] for line in events.splitlines() if line.startswith("id: "))
    replay = client.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": first_id}).text
    assert "event: run.queued" not in replay
    assert client.get(f"/api/runs/{rid}/events", headers={"Last-Event-ID": "bad"}).status_code == 400


def test_plugin_selection_settings_and_transactional_conflict(home, tmp_path):
    plugin = make_plugin(tmp_path)
    with pytest.raises(HarnessError):
        dispatch(home, "plugins.add", {"path": str(plugin), "trust": False})
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    before = init_home(home)["config"].read_bytes()
    assert dispatch(home, "tools.list", {"plugins": None}) == []
    assert dispatch(home, "tools.list", {"plugins": ["sample"]})[0]["name"] == "sample_echo"
    assert init_home(home)["config"].read_bytes() == before
    dispatch(home, "plugins.settings.set", {"id": "sample", "key": "prefix", "value": "new-"})
    with pytest.raises(HarnessError):
        dispatch(home, "plugins.settings.set", {"id": "sample", "key": "prefix", "value": 10})
    dispatch(home, "plugins.enable", {"id": "sample", "trust": True})
    assert len(dispatch(home, "tools.list", {})) == 1
    conflict = make_plugin(tmp_path, pid="other", tool="sample_echo")
    dispatch(home, "plugins.add", {"path": str(conflict), "trust": True})
    before = init_home(home)["config"].read_bytes()
    with pytest.raises(HarnessError) as error:
        PluginManager(home).build_registry(["sample", "other"])
    assert error.value.code == "tool_conflict"
    assert init_home(home)["config"].read_bytes() == before


def test_non_strict_plugin_schema_rejected(home, tmp_path):
    plugin = make_plugin(tmp_path, strict=False)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    with pytest.raises(HarnessError, match="additionalProperties"):
        PluginManager(home).build_registry(["sample"])
    assert load_config(home)["plugins"]["sample"]["trusted"] is True


def test_real_register_protocol_and_tool_loop(home, tmp_path):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    provider = Provider([completion(calls=[call()]), completion("tool-backed")])
    app = create_app(home, provider)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 200, response.text
        assert provider.requests[1][0][-2]["tool_calls"][0]["id"] == "call_1"
        tool_message = provider.requests[1][0][-1]
        assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"
        tool_result = json.loads(tool_message["content"])
        assert tool_result["session_id"] == response.headers["x-harness-session-id"]
        rid = response.headers["x-harness-run-id"]
        status = client.get(f"/api/runs/{rid}").json()
        assert status["tool_calls"] == 1
        assert any(event["type"] == "tool.intent" for event in status["events"])
        assert status["result"]["usage"]["total_tokens"] == 10


@pytest.mark.parametrize("calls,code", [([call(arguments='{"text":"x","extra":1}')], "invalid_tool_arguments"), ([call(arguments='{"text":"x","text":"y"}')], "invalid_tool_arguments"), ([call(name="unselected_run")], "unknown_tool"), ([call(), call(call_id="call_1")], "invalid_provider_response")])
def test_invalid_tool_batch_never_executes(home, tmp_path, calls, code):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    provider = Provider([completion(calls=calls)])
    with TestClient(create_app(home, provider)) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        if code == "invalid_provider_response":
            assert response.status_code == 502
            assert response.json()["error"]["code"] == code
        else:
            assert response.status_code == 200
            observation = provider.requests[1][0][-1]
            assert observation["tool_call_id"] == calls[0]["id"]
            assert json.loads(observation["content"])["error"]["code"] == code
        rid = response.headers["x-harness-run-id"]
        status = client.get(f"/api/runs/{rid}").json()
        assert status["tool_calls"] == 0
        assert not any(event["type"] == "tool.started" for event in status["events"])
        history = client.get(f"/api/sessions/{status['session_id']}/messages").json()
        assert [message["role"] for message in history] == (["user"] if code == "invalid_provider_response" else ["user", "assistant", "tool", "assistant"])


def test_idempotency_and_direct_tool_context(home, tmp_path):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    with TestClient(create_app(home, Provider())) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        direct = {"name": "sample_echo", "arguments": {"text": "hello"}, "harness_plugins": ["sample"]}
        first = client.post("/api/tools/call", headers={"Idempotency-Key": "same"}, json=direct)
        second = client.post("/api/tools/call", headers={"Idempotency-Key": "same"}, json=direct)
        assert first.status_code == second.status_code == 202
        assert first.json()["run_id"] == second.json()["run_id"]
        rid = first.json()["run_id"]
        client.get(f"/api/runs/{rid}/events")
        status = client.get(f"/api/runs/{rid}").json()
        assert status["result"]["result"]["session_id"] == "local-direct"
        assert status["result"]["result"]["task_id"] == rid
        assert client.get("/api/sessions").json() == []
        assert client.post("/api/tools/call", headers={"Idempotency-Key": "same"}, json={**direct, "arguments": {"text": "changed"}}).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
async def test_sync_tool_stop_retains_slot_until_thread_finishes(home, tmp_path, reason):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    if reason == "timeout":
        dispatch(home, "config.set", {"path": "run_timeout", "value": 0.04})
    store = Store(init_home(home)["database"])
    runs = RunManager(home, store, Provider())
    entered, release = threading.Event(), threading.Event()
    registry = PluginManager(home).build_registry(["sample"])
    def blocked(args, **kwargs):
        entered.set()
        release.wait(3)
        return json.dumps({"text": "drained"})
    registry.tools["sample_echo"].handler = blocked
    runs.plugins.build_registry = lambda *args, **kwargs: registry
    run = runs.submit({"name": "sample_echo", "arguments": {"text": "x"}, "harness_plugins": ["sample"]}, kind="tool")
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        if reason == "cancel":
            runs.cancel(run["run_id"])
        await asyncio.sleep(0.07)
        assert runs.busy
        assert store.run(run["run_id"])["status"] in ("cancelling", "timing_out")
        with pytest.raises(HarnessError) as error:
            runs.submit(body())
        assert error.value.status == 409
        release.set()
        finished = await runs.wait(run["run_id"])
        assert finished["status"] == ("cancelled" if reason == "cancel" else "timed_out")
        assert finished["tool_calls"] == 1
        assert not runs.busy
    finally:
        release.set()
        await runs.drain()
        store.close()


@pytest.mark.asyncio
async def test_busy_queries_and_config_mutation_blocked(home):
    entered, release = asyncio.Event(), asyncio.Event()
    class Slow:
        async def complete(self, messages, tools, max_tokens):
            entered.set()
            await release.wait()
            return completion()
    import httpx
    app = create_app(home, Slow())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer " + init_home(home)["token"].read_text().strip()}) as client:
            first = await client.post("/api/runs", json=body(), headers={"Idempotency-Key": "busy-idem"})
            await entered.wait()
            assert (await client.get("/api/status")).json()["busy"] is True
            assert (await client.get("/api/config")).status_code == 200
            assert (await client.patch("/api/config", json={"path": "rounds", "value": 4})).status_code == 409
            assert (await client.post("/api/runs", json=body())).status_code == 409
            same = await client.post("/api/runs", json=body(), headers={"Idempotency-Key": "busy-idem"})
            assert same.json()["run_id"] == first.json()["run_id"]
            release.set()
            await app.state.runs.wait(first.json()["run_id"])


def test_shutdown_callback_and_instance_guard(home):
    app = create_app(home, Provider())
    called = threading.Event()
    app.state.request_shutdown = called.set
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        assert client.post("/api/shutdown", headers={"X-Harness-Instance-Id": "wrong"}).status_code == 409
        assert client.post("/api/shutdown").json()["status"] == "draining"
        assert called.wait(1)
        assert client.post("/api/runs", json=body()).status_code == 503
    assert not init_home(home)["owner_descriptor"].exists()


def test_boot_recovery_does_not_retry_uncertain_side_effect(home):
    store = Store(init_home(home)["database"])
    sid = store.create_session()["id"]
    store.create_run("run_dead", "chat", sid, body(), "fingerprint", "recover-key")
    intent = store.intent("run_dead", "call_dead", "sample_echo", {"text": "x"})
    store.update_intent(intent, "started")
    store.update_run("run_dead", status="running")
    store.close()
    provider = Provider()
    with TestClient(create_app(home, provider)) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        recovered = client.get("/api/runs/run_dead").json()
        assert recovered["status"] == "interrupted"
        assert provider.requests == []
        assert client.get(f"/api/sessions/{sid}/messages").json() == []


@pytest.mark.parametrize("text", ['{"x":NaN}', '{"x":1,"x":2}', '{"x":Infinity}'])
def test_strict_json(text):
    with pytest.raises(ValueError):
        parse_json(text)


@pytest.mark.parametrize("raw", ['{"model":"harness-default","model":"x","messages":[]}', '{"model":"harness-default","messages":[],"x":NaN}', '{"model":"harness-default","messages":[],"x":1e999}'])
def test_raw_request_strict_json(client, raw):
    response = client.post("/api/runs", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_business_error_keeps_loop_alive(home, tmp_path):
    source = '''import json

def register(ctx):
    def handler(args, **kwargs):
        return json.dumps({"error": {"code": "domain_error", "message": "An expected business error", "execution": "not_started"}})
    ctx.register_tool("sample_echo", "sample", {"name": "sample_echo", "description": "Echo", "parameters": {"type": "object", "additionalProperties": False}}, handler)
'''
    plugin = make_plugin(tmp_path, source=source)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    provider = Provider([completion(calls=[call(arguments="{}")]), completion("Handled domain error")])
    with TestClient(create_app(home, provider)) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 200
        assert json.loads(provider.requests[1][0][-1]["content"])["error"]["code"] == "domain_error"
        rid = response.headers["x-harness-run-id"]
        assert client.get(f"/api/runs/{rid}").json()["tool_calls"] == 1


@pytest.mark.parametrize("limit,code", [("rounds", "round_limit"), ("tools", "tool_limit")])
def test_budgets_stop_without_hidden_execution(home, tmp_path, limit, code):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    dispatch(home, "config.set", {"path": limit, "value": 1})
    provider = Provider([completion(calls=[call()]), completion(calls=[call(call_id="call_2")])])
    with TestClient(create_app(home, provider)) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
        rid = response.headers["x-harness-run-id"]
        assert client.get(f"/api/runs/{rid}").json()["tool_calls"] == 1


def test_provider_exception_is_not_logged_or_returned(home):
    class Broken:
        async def complete(self, messages, tools, max_tokens):
            raise RuntimeError("synthetic-secret-that-must-not-leak")
    with TestClient(create_app(home, Broken())) as client:
        client.headers["Authorization"] = "Bearer " + init_home(home)["token"].read_text().strip()
        response = client.post("/v1/chat/completions", json=body())
        assert response.status_code == 502
        rid = response.headers["x-harness-run-id"]
        assert "synthetic-secret" not in response.text
        assert "synthetic-secret" not in client.get(f"/api/runs/{rid}/events").text


def test_owner_lock_rejects_second_app(home):
    first, second = create_app(home, Provider()), create_app(home, Provider())
    with TestClient(first):
        with pytest.raises(HarnessError) as error:
            with TestClient(second):
                pass
        assert error.value.code == "owner_conflict"


def test_json_overflow_rejected():
    with pytest.raises(ValueError):
        parse_json('{"value": 1e999}')


def test_both_independent_plugins_register_without_browser_launch(home, monkeypatch):
    monkeypatch.delenv("HARNESS_HOME", raising=False)
    project = Path(__file__).resolve().parents[1]
    for pid in ("arithmetic", "browser-safety"):
        dispatch(home, "plugins.add", {"path": str(project / "plugins" / pid), "trust": True})
    registry = PluginManager(home).build_registry(["arithmetic", "browser-safety"])
    assert "arithmetic_parse" in registry.tools
    assert "browser_open" in registry.tools
    assert len(registry.tools) == 12
    assert (home / "plugin-data" / "browser-safety").is_dir()
    assert not (home / "plugin-data" / "browser-safety" / "runtime.json").exists()
