"""Bounded protocol regressions. Only temporary homes, fake providers and ASGI/MockTransport."""
from __future__ import annotations
import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIError, AsyncOpenAI
from harness.errors import HarnessError
from harness.management import dispatch
from harness.paths import init_home, load_config
from harness.provider import OpenAIProvider
from harness.runs import RunManager
from harness.server import create_app
from harness.store import Store
from test_core import Provider, body, call, completion, home, make_plugin


def auth(home):
    return {"Authorization": "Bearer " + init_home(home)["token"].read_text().strip()}


def test_same_wire_id_across_rounds_and_rejected_tool_pairing(home, tmp_path):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    provider = Provider([completion(calls=[call(name="missing_tool")]), completion(calls=[call()]), completion(calls=[call()]), completion("corrected")])
    with TestClient(create_app(home, provider)) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 200, response.text
        rid, sid = response.headers["x-harness-run-id"], response.headers["x-harness-session-id"]
        history = client.get(f"/api/sessions/{sid}/messages").json()
        assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant"]
        assert all(m["tool_call_id"] == "call_1" for m in history if m["role"] == "tool")
        assert json.loads(history[2]["content"])["error"]["execution"] == "not_started"
        assert client.get(f"/api/runs/{rid}").json()["tool_calls"] == 2
        rows = client.app.state.store.connection.execute("SELECT tool_call_id,status FROM intents WHERE run_id=? ORDER BY id", (rid,)).fetchall()
        assert [tuple(row) for row in rows] == [("round-2-call-0", "finished"), ("round-3-call-0", "finished")]


@pytest.mark.parametrize("invalid", [call(), {"id": "bad", "type": "function", "function": {"name": "sample_echo"}}, {"id": "bad", "type": "other", "function": {"name": "sample_echo", "arguments": "{}"}}])
def test_later_malformed_call_rejects_entire_batch(home, tmp_path, invalid):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    with TestClient(create_app(home, Provider([completion(calls=[call(), invalid])]))) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 502
        run = client.get("/api/runs/" + response.headers["x-harness-run-id"]).json()
        assert run["tool_calls"] == 0
        assert not any(e["type"].startswith("tool.") for e in run["events"])
        assert client.get(f"/api/sessions/{run['session_id']}/messages").json() == body()["messages"]


@pytest.mark.asyncio
async def test_history_is_durable_before_next_provider_request(home, tmp_path):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    store = Store(init_home(home)["database"])
    class Inspect(Provider):
        async def complete(self, messages, tools, max_tokens):
            assert store.messages(runs.active and store.run(runs.active.id)["session_id"]) == messages
            return await super().complete(messages, tools, max_tokens)
    runs = RunManager(home, store, Inspect([completion(calls=[call()]), completion()]))
    try:
        accepted = runs.submit(body(harness_plugins=["sample"]))
        run = await runs.wait(accepted["run_id"])
        assert run["status"] == "completed", run
        history = store.messages(run["session_id"])
        assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
        result = store.connection.execute("SELECT result FROM intents WHERE run_id=?", (run["id"],)).fetchone()[0]
        assert result == history[2]["content"]
    finally:
        await runs.drain()
        store.close()


def test_crash_started_without_result_is_unknown_and_session_blocked(home):
    store = Store(init_home(home)["database"])
    sid = store.create_session()["id"]
    store.create_run("run_crash", "chat", sid, body(), "fp", None)
    store.commit_messages(sid, "run_crash", body()["messages"] + [{"role": "assistant", "content": None, "tool_calls": [call(), call(call_id="call_2")]}])
    started = store.intent("run_crash", "round-1-call-0", "sample_echo", {"text": "x"})
    store.update_intent(started, "started")
    store.intent("run_crash", "round-1-call-1", "sample_echo", {"text": "y"})
    store.close()
    provider = Provider()
    with TestClient(create_app(home, provider)) as client:
        client.headers.update(auth(home))
        assert client.get("/api/runs/run_crash").json()["status"] == "interrupted"
        assert client.get(f"/api/sessions/{sid}").json()["status"] == "interrupted"
        assert client.post("/v1/chat/completions", headers={"X-Harness-Session-Id": sid}, json=body()).status_code == 409
        rows = client.app.state.store.connection.execute("SELECT status,result FROM intents ORDER BY id").fetchall()
        assert [tuple(row) for row in rows] == [("unknown", None), ("not_started", None)]
        assert provider.requests == []
        assert len(client.get(f"/api/sessions/{sid}/messages").json()) == 2


@pytest.mark.parametrize("token", ["", "short", "1234567890abcdefghijklmnopqrstuv"[:31], "x" * 64, "ab " * 20])
def test_weak_tokens_refuse_app_creation(home, token):
    init_home(home)["token"].write_text(token)
    with pytest.raises(HarnessError) as caught:
        create_app(home, Provider())
    assert caught.value.code == "invalid_local_token"


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://localhost", ""])
def test_origin_rejected_even_with_valid_auth(home, origin):
    with TestClient(create_app(home, Provider())) as client:
        client.headers.update(auth(home))
        for path in ("/health", "/api/config", "/v1/models"):
            response = client.get(path, headers={"Origin": origin})
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "origin_forbidden"


def test_only_published_alias_is_admitted(home):
    provider = Provider()
    with TestClient(create_app(home, provider)) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body(model=load_config(home)["model"]))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"
        assert provider.requests == []


@pytest.mark.parametrize("stream", [False, True])
def test_length_retained_as_incomplete_and_missing_usage_is_null(home, stream):
    raw = completion("partial")
    raw.pop("usage")
    raw["choices"][0]["finish_reason"] = "length"
    with TestClient(create_app(home, Provider([raw]))) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body(stream=stream, **({"stream_options": {"include_usage": True}} if stream else {})))
        assert response.status_code == 200
        run = client.get("/api/runs/" + response.headers["x-harness-run-id"]).json()
        assert run["status"] == "incomplete"
        assert run["result"]["choices"][0]["finish_reason"] == "length"
        assert run["result"]["usage"] is None
        if stream:
            chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
            assert chunks[-2]["choices"][0]["finish_reason"] == "length"
            assert chunks[-1]["usage"] is None
        else:
            assert response.json()["usage"] is None
        assert "event: run.incomplete" in client.get(run["events_url"]).text


@pytest.mark.asyncio
async def test_registry_is_loaded_after_slot_admission_off_event_loop(home):
    dispatch(home, "config.set", {"path": "run_timeout", "value": 0.04})
    store = Store(init_home(home)["database"])
    runs = RunManager(home, store, Provider())
    entered, release = threading.Event(), threading.Event()
    original = runs.plugins.build_registry
    def blocked(*args, **kwargs):
        assert runs.busy
        entered.set()
        release.wait(3)
        return original(*args, **kwargs)
    runs.plugins.build_registry = blocked
    try:
        accepted = runs.submit(body())
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        await asyncio.sleep(0.06)
        assert store.run(accepted["run_id"])["status"] == "timing_out"
        assert runs.busy
        with pytest.raises(HarnessError) as caught:
            runs.submit(body())
        assert caught.value.code == "harness_busy"
        release.set()
        assert (await runs.wait(accepted["run_id"]))["status"] == "timed_out"
        assert runs.provider.requests == []
    finally:
        release.set()
        await runs.drain()
        store.close()


@pytest.mark.asyncio
async def test_sse_role_first_chunk_arrives_before_model_finishes_without_network(home, monkeypatch):
    import time
    import harness.server as server_module
    ticks = iter(range(0, 10000, 11))
    monkeypatch.setattr(server_module, "time", SimpleNamespace(time=time.time, monotonic=lambda: next(ticks)))
    entered, release, first, heartbeat = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Blocked:
        async def complete(self, messages, tools, max_tokens):
            entered.set()
            await release.wait()
            return completion("after release")
    app = create_app(home, Blocked())
    payload = json.dumps(body(stream=True)).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"), (b"authorization", auth(home)["Authorization"].encode())], "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    sent, consumed = [], False
    async def receive():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()
    async def send(message):
        sent.append(copy.deepcopy(message))
        if message["type"] == "http.response.body" and b'"role": "assistant"' in message.get("body", b""):
            first.set()
        if message["type"] == "http.response.body" and b": heartbeat" in message.get("body", b""):
            heartbeat.set()
    async with app.router.lifespan_context(app):
        request_task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(first.wait(), 1)
            assert not release.is_set()
            assert not request_task.done()
            assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 200
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(heartbeat.wait(), 1)
            assert not request_task.done()
            release.set()
            await asyncio.wait_for(request_task, 2)
            wire = b"".join(m.get("body", b"") for m in sent)
            assert b"after release" in wire and b"[DONE]" in wire
        finally:
            release.set()
            await asyncio.gather(request_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_openai_sdk_receives_real_sse_error_not_fake_stop(home):
    class Broken:
        async def complete(self, *args):
            raise RuntimeError("synthetic-secret-not-to-leak")
    app = create_app(home, Broken())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with AsyncOpenAI(api_key=auth(home)["Authorization"][7:], base_url="http://testserver/v1", max_retries=0, http_client=httpx.AsyncClient(transport=transport)) as sdk:
            stream = await sdk.chat.completions.create(model="harness-default", messages=body()["messages"], stream=True)
            first = await stream.__anext__()
            assert first.choices[0].delta.role == "assistant"
            with pytest.raises(APIError) as caught:
                await stream.__anext__()
            assert "synthetic-secret" not in str(caught.value)
            assert caught.value.body["code"] == "internal_error"
            await stream.close()


@pytest.mark.asyncio
async def test_provider_public_httpx_wire_routing_and_cost_preserved(home):
    seen = []
    async def mock(request):
        seen.append(json.loads(request.content))
        raw = completion()
        raw.update({"id": "chatcmpl-mock", "object": "chat.completion", "created": 1, "model": "mock"})
        raw["usage"]["cost"] = 0.125
        return httpx.Response(200, json=raw)
    provider = OpenAIProvider(home, load_config(home))
    provider.client = AsyncOpenAI(api_key="synthetic-test-only", base_url="https://mock.invalid/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock)))
    try:
        raw = await provider.complete(body()["messages"], [], 128)
        assert raw["usage"]["cost"] == 0.125
        assert seen[0]["provider"] == {"require_parameters": True, "allow_fallbacks": False}
        assert seen[0]["model"] == load_config(home)["model"]
        assert len(seen) == 1
        assert provider.deadline == 45
    finally:
        await provider.close()
    with TestClient(create_app(home, Provider([raw]))) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body())
        run = client.get("/api/runs/" + response.headers["x-harness-run-id"]).json()
        assert run["result"]["usage"]["cost"] == 0.125
        assert run["cost"] == 0.125
        assert next(e for e in run["events"] if e["type"] == "run.completed")["data"]["cost"] == 0.125


@pytest.mark.parametrize("status,code", [(401, "provider_authentication_error"), (403, "provider_authentication_error"), (429, "provider_rate_limit"), (400, "provider_request_rejected"), (503, "provider_unavailable")])
@pytest.mark.asyncio
async def test_upstream_errors_are_classified_redacted_and_not_retried(home, status, code):
    requests = []
    async def mock(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic-upstream-secret", "code": "private"}})
    provider = OpenAIProvider(home, load_config(home))
    provider.client = AsyncOpenAI(api_key="synthetic-test-only", base_url="https://mock.invalid/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(mock)))
    try:
        with pytest.raises(HarnessError) as caught:
            await provider.complete(body()["messages"], [], 128)
        assert caught.value.code == code
        assert "synthetic-upstream-secret" not in json.dumps(caught.value.body())
        assert len(requests) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_provider_whole_request_deadline_even_if_transport_ignores_timeout(home):
    config = load_config(home)
    config["run_timeout"] = 0.02
    provider = OpenAIProvider(home, config)
    async def blocked(**kwargs):
        await asyncio.Event().wait()
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=blocked)))
    with pytest.raises(HarnessError) as caught:
        await provider.complete(body()["messages"], [], 128)
    assert caught.value.code == "provider_timeout"
    assert provider.deadline == 0.02


def test_startup_and_management_reads_never_import_or_reregister_plugins(home, tmp_path):
    marker = tmp_path / "registered.txt"
    imported = tmp_path / "imported.txt"
    source = f'''import json
from pathlib import Path
Path({str(imported)!r}).write_text("imported")
def register(ctx):
    p = Path({str(marker)!r})
    p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")
    ctx.register_tool("sample_echo", "sample", {{"name": "sample_echo", "description": "Echo", "parameters": {{"type": "object", "additionalProperties": False}}}}, lambda args, **kwargs: json.dumps({{"ok": True}}))
'''
    plugin = make_plugin(tmp_path, source=source)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    assert not marker.exists() and not imported.exists()
    dispatch(home, "plugins.enable", {"id": "sample", "trust": True})
    baseline = int(marker.read_text()) if marker.exists() else 0
    with TestClient(create_app(home, Provider())) as client:
        client.headers.update(auth(home))
        for path in ("/api/status", "/api/plugins", "/api/plugins/sample", "/api/tools", "/api/config"):
            assert client.get(path).status_code == 200
        assert (int(marker.read_text()) if marker.exists() else 0) == baseline
        assert client.post("/v1/chat/completions", json=body()).status_code == 200
        assert int(marker.read_text()) == baseline + 1
        for _ in range(2):
            for path in ("/api/plugins", "/api/plugins/sample", "/api/tools?harness_plugins=%5B%22sample%22%5D", "/api/config"):
                assert client.get(path).status_code == 200
        assert int(marker.read_text()) == baseline + 1


@pytest.mark.asyncio
async def test_production_client_configuration_is_public_httpx_no_retries(home, monkeypatch):
    import openai
    captured = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-only")
    async def create(**kwargs):
        captured["request"] = kwargs
        return SimpleNamespace(model_dump=lambda **kwargs: completion())
    async def close():
        await captured["http_client"].aclose()
    def factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close)
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    provider = OpenAIProvider(home, load_config(home))
    try:
        await provider.complete(body()["messages"], [], 128)
        assert captured["max_retries"] == 0
        assert captured["timeout"] == 45
        assert isinstance(captured["http_client"], httpx.AsyncClient)
        assert captured["request"]["extra_body"]["provider"] == {"require_parameters": True, "allow_fallbacks": False}
    finally:
        await provider.close()


def test_failed_terminal_run_retains_observed_cost_and_no_fabricated_usage(home, tmp_path):
    plugin = make_plugin(tmp_path)
    dispatch(home, "plugins.add", {"path": str(plugin), "trust": True})
    dispatch(home, "config.set", {"path": "rounds", "value": 1})
    raw = completion(calls=[call()])
    raw["usage"]["cost"] = 0.25
    with TestClient(create_app(home, Provider([raw]))) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body(harness_plugins=["sample"]))
        assert response.status_code == 422
        run = client.get("/api/runs/" + response.headers["x-harness-run-id"]).json()
        assert run["status"] == "failed"
        assert run["cost"] == 0.25
        assert run["result"] is None
        assert "synthetic-test-only" not in json.dumps(run)


def test_partial_usage_reports_unknown_fields_as_null(home):
    raw = completion()
    raw["usage"] = {"prompt_tokens": 3, "cost": 0.1}
    with TestClient(create_app(home, Provider([raw]))) as client:
        client.headers.update(auth(home))
        response = client.post("/v1/chat/completions", json=body())
        assert response.status_code == 200
        assert response.json()["usage"] == {"prompt_tokens": 3, "completion_tokens": None, "total_tokens": None, "cost": 0.1}
