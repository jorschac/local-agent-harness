"""Cross-module acceptance tests: SDK -> daemon app -> dynamic plugin -> model.
No real model key or external browser is used here.
"""
import asyncio
import copy
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

PROJECT = Path(__file__).resolve().parents[1]


def completion(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"id": "mock", "object": "chat.completion", "created": 1,
            "model": "mock", "choices": [{"index": 0, "message": message,
            "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class ArithmeticProvider:
    def __init__(self):
        self.requests = []

    async def complete(self, messages, tools, max_tokens):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        tool_messages = [m for m in messages if m["role"] == "tool"]
        if not tool_messages:
            names = {t["function"]["name"] for t in tools}
            assert "arithmetic_parse" in names
            return completion(calls=[{"id": "call_parse", "type": "function", "function": {
                "name": "arithmetic_parse", "arguments": json.dumps({"expression": "(35 × 7 - 128) ÷ (12 + 8)"})}}])
        assert tool_messages[-1]["tool_call_id"] == "call_parse"
        result = json.loads(tool_messages[-1]["content"])
        assert not result.get("error"), result
        return completion("使用运算插件得到 5.85。")


class TextProvider:
    async def complete(self, messages, tools, max_tokens):
        return completion("你好，测试完成。")


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))
    from harness.paths import init_home
    from harness.server import create_app
    init_home(tmp_path)
    token = (tmp_path / "secrets/local-token").read_text().strip()
    return lambda provider: (create_app(home=tmp_path, provider=provider), token, tmp_path)


def test_sdk_plugin_roundtrip_and_one_shot_no_config_write(app_factory):
    provider = ArithmeticProvider()
    app, token, home = app_factory(provider)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        added = client.post("/api/plugins", headers=headers, json={
            "path": str(PROJECT / "plugins/arithmetic"), "trust": True, "replace": False})
        assert added.status_code < 300, added.text
        before = (home / "config.yaml").read_bytes()
        sdk = OpenAI(base_url="http://testserver/v1", api_key=token,
                     http_client=client, max_retries=0)
        answer = sdk.chat.completions.with_raw_response.create(
            model="harness-default", messages=[{"role": "user", "content": "用插件计算算式。"}],
            extra_body={"harness_plugins": ["arithmetic"]},
            extra_headers={"Idempotency-Key": "acceptance-math-001"})
        assert "5.85" in answer.parse().choices[0].message.content
        assert answer.headers.get("x-harness-run-id")
        assert answer.headers.get("x-harness-session-id")
        assert len(provider.requests) == 2
        assert (home / "config.yaml").read_bytes() == before
        roles = [m["role"] for m in provider.requests[1]["messages"]]
        assert roles[-2:] == ["assistant", "tool"]


def test_standard_sdk_sse_and_unsupported_external_tools(app_factory):
    app, token, _ = app_factory(TextProvider())
    with TestClient(app) as client:
        sdk = OpenAI(base_url="http://testserver/v1", api_key=token,
                     http_client=client, max_retries=0)
        chunks = list(sdk.chat.completions.create(model="harness-default",
            messages=[{"role": "user", "content": "你好"}], stream=True,
            stream_options={"include_usage": True}))
        content = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        assert "测试完成" in content
        assert any(c.usage is not None for c in chunks)
        rejected = client.post("/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"}, json={"model": "harness-default",
            "messages": [{"role": "user", "content": "你好"}], "tools": []})
        assert rejected.status_code == 400
        assert "error" in rejected.json()
        assert client.get("/api/plugins").status_code == 401


def test_session_history_has_single_authority(app_factory):
    app, token, _ = app_factory(TextProvider())
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        body = {"model": "harness-default", "messages": [{"role": "user", "content": "第一轮"}]}
        first = client.post("/v1/chat/completions", headers=headers, json=body)
        assert first.status_code == 200, first.text
        session = first.headers["x-harness-session-id"]
        next_headers = {**headers, "X-Harness-Session-Id": session}
        second = client.post("/v1/chat/completions", headers=next_headers,
            json={"model": "harness-default", "messages": [{"role": "user", "content": "第二轮"}]})
        assert second.status_code == 200, second.text
        assert second.headers["x-harness-session-id"] == session
        bad = client.post("/v1/chat/completions", headers=next_headers,
            json={"model": "harness-default", "messages": [
                {"role": "user", "content": "第一轮"}, {"role": "assistant", "content": "旧历史"},
                {"role": "user", "content": "第二轮"}]})
        assert bad.status_code == 400
