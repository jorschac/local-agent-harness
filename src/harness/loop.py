from __future__ import annotations
import json
import math
import time
from .errors import HarnessError
from .registry import parse_json


class StopRequested(Exception):
    pass


class AgentLoop:
    def __init__(self, provider, registry, store, run_id: str, session_id: str, config: dict, stop, user_task: str):
        self.provider, self.registry, self.store = provider, registry, store
        self.run_id, self.session_id, self.config, self.stop, self.user_task = run_id, session_id, config, stop, user_task
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.cost = None
        self.usage_missing = False

    def checkpoint(self):
        if self.stop.is_set():
            raise StopRequested()

    async def tool(self, name: str, arguments: dict, call_id: str, *, intent_key=None, observation=False) -> str:
        self.checkpoint()
        self.registry.validate_call(name, arguments)
        if self.calls >= self.config["tools"]:
            raise HarnessError("Tool-call budget exhausted", "tool_limit", 422)
        intent = self.store.intent(self.run_id, intent_key or call_id, name, arguments)
        self.store.event(self.run_id, "tool.intent", {"call_id": call_id, "name": name, "arguments": arguments, "intent_id": intent})
        def started():
            self.checkpoint()
            self.calls += 1
            self.store.update_intent(intent, "started")
            self.store.update_run(self.run_id, tool_calls=self.calls)
            self.store.event(self.run_id, "tool.started", {"call_id": call_id, "name": name, "tool_calls": self.calls})
        result = await self.registry.execute(name, arguments, session_id=self.session_id, task_id=self.run_id, user_task=self.user_task, on_start=started)
        # The result and its wire observation are durable before cancellation or another model request.
        self.store.finish_tool(self.session_id, self.run_id, intent, result, call_id if observation else None)
        self.store.event(self.run_id, "tool.result", {"call_id": call_id, "name": name, "result": parse_json(result)})
        self.checkpoint()
        return result

    def add_usage(self, raw):
        usage = raw.get("usage")
        if usage is None:
            self.usage_missing = True
            return
        if not isinstance(usage, dict):
            raise HarnessError("Invalid provider usage", "invalid_provider_response", 502)
        for key in self.usage:
            value = usage.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise HarnessError("Invalid provider usage", "invalid_provider_response", 502)
            if value is None:
                self.usage[key] = None
            elif self.usage[key] is not None:
                self.usage[key] += value
        cost = usage.get("cost")
        if cost is not None:
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise HarnessError("Invalid provider usage cost", "invalid_provider_response", 502)
            self.cost = (self.cost or 0) + cost

    async def run(self, messages: list[dict], max_tokens: int) -> tuple[dict, list[dict]]:
        messages = [dict(message) for message in messages]
        initial_length = len(messages)
        for round_index in range(self.config["rounds"]):
            self.checkpoint()
            self.store.event(self.run_id, "provider.request", {"round": round_index + 1, "max_tokens": max_tokens, "tool_names": list(self.registry.tools)})
            raw = await self.provider.complete(messages, self.registry.descriptions(), max_tokens)
            self.checkpoint()
            if not isinstance(raw, dict) or not isinstance(raw.get("choices"), list) or len(raw["choices"]) != 1:
                raise HarnessError("Provider returned an invalid completion", "invalid_provider_response", 502)
            choice = raw["choices"][0]
            if not isinstance(choice, dict):
                raise HarnessError("Invalid provider choice", "invalid_provider_response", 502)
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise HarnessError("Provider must return an assistant message", "invalid_provider_response", 502)
            self.add_usage(raw)
            calls = message.get("tool_calls", [])
            if calls is None:
                calls = []
            content = message.get("content")
            if not isinstance(calls, list) or (content is not None and not isinstance(content, str)):
                raise HarnessError("Invalid provider message", "invalid_provider_response", 502)
            finish = choice.get("finish_reason")
            if calls:
                if finish != "tool_calls" or len(calls) > self.config["tools"] - self.calls:
                    raise HarnessError("Invalid or over-budget tool batch", "tool_limit" if len(calls) > self.config["tools"] - self.calls else "invalid_provider_response", 422)
                normalized, parsed_calls, batch_ids = [], [], set()
                # Malformed structure or duplicate IDs reject the COMPLETE batch, with no tool side effects.
                # IDs are scoped to this batch; observations are paired by position, never a global ID map.
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in batch_ids:
                        raise HarnessError("Invalid/duplicate provider tool call", "invalid_provider_response", 502)
                    function = call.get("function")
                    if not isinstance(function, dict) or set(function) != {"name", "arguments"} or not isinstance(function["name"], str) or not function["name"] or not isinstance(function["arguments"], str):
                        raise HarnessError("Invalid provider function call", "invalid_provider_response", 502)
                    batch_ids.add(call["id"])
                    error = None
                    try:
                        args = parse_json(function["arguments"])
                    except (ValueError, TypeError, RecursionError):
                        args = None
                        error = HarnessError("Provider tool arguments are not strict JSON", "invalid_tool_arguments")
                    if error is None:
                        try:
                            self.registry.validate_call(function["name"], args)
                        except HarnessError as exc:
                            if exc.code not in ("unknown_tool", "invalid_tool_arguments"):
                                raise
                            error = exc
                    normalized.append({"id": call["id"], "type": "function", "function": dict(function)})
                    parsed_calls.append((call["id"], function["name"], args, error))
                declaration = {"role": "assistant", "content": content, "tool_calls": normalized}
                self.store.commit_messages(self.session_id, self.run_id, [declaration])
                messages.append(declaration)
                for call_index, (call_id, name, args, error) in enumerate(parsed_calls):
                    if error is not None:
                        result = json.dumps({"error": {**error.body()["error"], "execution": "not_started"}}, ensure_ascii=False)
                        self.store.commit_messages(self.session_id, self.run_id, [{"role": "tool", "tool_call_id": call_id, "content": result}])
                        self.store.event(self.run_id, "tool.rejected", {"call_id": call_id, "name": name, "result": parse_json(result)})
                    else:
                        result = await self.tool(name, args, call_id, intent_key=f"round-{round_index + 1}-call-{call_index}", observation=True)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                continue
            if finish not in ("stop", "length") or not isinstance(content, str) or not content:
                raise HarnessError("Provider did not return final text", "invalid_provider_response", 502)
            final = {"role": "assistant", "content": content}
            self.store.commit_messages(self.session_id, self.run_id, [final])
            messages.append(final)
            usage = None if self.usage_missing else dict(self.usage)
            if usage is not None and self.cost is not None:
                usage["cost"] = self.cost
            response = {"id": "chatcmpl-" + self.run_id, "object": "chat.completion", "created": int(time.time()), "model": self.config["alias"], "choices": [{"index": 0, "message": final, "finish_reason": finish}], "usage": usage}
            self.store.event(self.run_id, "assistant.final", {"content": content, "finish_reason": finish})
            return response, messages[initial_length:]
        raise HarnessError("Model round budget exhausted before final text", "round_limit", 422)
