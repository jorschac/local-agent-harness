from __future__ import annotations
import asyncio
import inspect
import json
import os
import re
from dataclasses import dataclass
from typing import Callable
from jsonschema import Draft202012Validator, ValidationError
from .errors import HarnessError


def strict_object_schemas(schema: dict) -> None:
    if not isinstance(schema, dict):
        raise HarnessError("Tool schema must be an object", "invalid_plugin")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise HarnessError("Invalid tool JSON schema", "invalid_plugin") from None
    def visit(node):
        if isinstance(node, dict):
            if any(key in node for key in ("$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not")):
                raise HarnessError("Referenced/composed schemas are unsupported", "invalid_plugin")
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") is not False:
                    raise HarnessError("Every tool object schema must set additionalProperties:false", "invalid_plugin")
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)


def parse_json(text: str):
    def reject_constant(value):
        raise ValueError("Non-finite JSON number")
    def finite_float(value):
        import math
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number")
        return parsed
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_pairs)


@dataclass
class Tool:
    name: str
    plugin_id: str
    toolset: str
    schema: dict
    handler: Callable
    is_async: bool
    check_fn: Callable | None = None
    requires_env: tuple = ()

    def public(self) -> dict:
        return {"type": "function", "function": self.schema}


class Registry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def descriptions(self) -> list[dict]:
        return [tool.public() for tool in self.tools.values()]

    def list(self) -> list[dict]:
        return [{"name": tool.name, "plugin_id": tool.plugin_id, "toolset": tool.toolset, "schema": tool.schema, "is_async": tool.is_async, "requires_env": list(tool.requires_env)} for tool in self.tools.values()]

    def add(self, tool: Tool):
        if tool.name in self.tools:
            raise HarnessError("Duplicate tool name: " + tool.name, "tool_conflict", 409)
        self.tools[tool.name] = tool

    def validate_call(self, name: str, arguments: dict) -> Tool:
        tool = self.tools.get(name)
        if tool is None:
            raise HarnessError("Unknown or unselected tool", "unknown_tool", param="name")
        if not isinstance(arguments, dict):
            raise HarnessError("Tool arguments must be an object", "invalid_tool_arguments", param="arguments")
        try:
            Draft202012Validator(tool.schema["parameters"]).validate(arguments)
        except ValidationError:
            raise HarnessError("Tool arguments do not match its schema", "invalid_tool_arguments", param="arguments") from None
        # Also rejects NaN and exotic Python objects on direct calls.
        try:
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError):
            raise HarnessError("Tool arguments must be strict JSON", "invalid_tool_arguments") from None
        return tool

    async def execute(self, name: str, arguments: dict, *, session_id: str, task_id: str, user_task: str, on_start=None) -> str:
        tool = self.validate_call(name, arguments)
        if any(not os.environ.get(key) for key in tool.requires_env):
            return json.dumps({"error": {"code": "missing_environment", "message": "Required plugin environment is not configured", "execution": "not_started"}})
        try:
            if tool.check_fn is not None:
                check = await asyncio.to_thread(tool.check_fn)
                if inspect.isawaitable(check):
                    check = await check
                if not check:
                    return json.dumps({"error": {"code": "tool_unavailable", "message": "Tool availability check failed", "execution": "not_started"}})
            if on_start is not None:
                on_start()
            kwargs = {"session_id": session_id, "task_id": task_id, "user_task": user_task}
            signature = inspect.signature(tool.handler)
            if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
                kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
            if tool.is_async:
                result = await tool.handler(arguments, **kwargs)
            else:
                # Never cancel this future to pretend a non-cancellable thread finished.
                result = await asyncio.to_thread(tool.handler, arguments, **kwargs)
            if not isinstance(result, str):
                return json.dumps({"error": {"code": "tool_result_contract", "message": "Handler must return a string", "execution": "completed"}})
            if len(result.encode("utf-8")) > 16384:
                return json.dumps({"error": {"code": "tool_result_too_large", "message": "Result exceeds 16 KiB; tool already executed", "execution": "completed"}})
            try:
                parsed = parse_json(result)
            except ValueError:
                parsed = {"text": result}
            return json.dumps(parsed, ensure_ascii=False, allow_nan=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            return json.dumps({"error": {"code": "tool_execution_error", "message": "Plugin execution failed", "execution": "failed"}})


class PluginContext:
    def __init__(self, plugin_id: str, manifest: dict, settings: dict, registry: Registry, home=None):
        self.plugin_id = plugin_id
        self._manifest, self._settings, self._registry = manifest, settings, registry
        self._home = home

    def get_config(self, key: str, default=None):
        spec = self._manifest.get("config_schema", {}).get(key, {})
        # Generic local-state convention, only when declared and without a default.
        if key == "state_dir" and key in self._manifest.get("config_schema", {}) and key not in self._settings and "default" not in spec and self._home is not None:
            return str(self._home / "plugin-data" / self.plugin_id)
        return self._settings.get(key, default)

    def set_config(self, key, value):
        raise HarnessError("Plugin self-write is unsupported; use the CLI", "unsupported_plugin_api")

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description='', emoji='', override=False):
        if override:
            raise HarnessError("Tool override is not supported", "tool_conflict", 409)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) or "_" not in name or not isinstance(toolset, str) or not toolset:
            raise HarnessError("Tool names must carry a namespace prefix", "invalid_plugin")
        if name not in self._manifest["provides_tools"]:
            raise HarnessError("Tool is not declared in provides_tools", "invalid_plugin")
        if not isinstance(schema, dict) or set(schema) - {"name", "description", "parameters"} or schema.get("name") != name or not isinstance(schema.get("description", description), str):
            raise HarnessError("Invalid inner function schema", "invalid_plugin")
        function = dict(schema)
        function.setdefault("description", description)
        params = function.get("parameters")
        strict_object_schemas(params)
        if params.get("type") != "object" or not callable(handler):
            raise HarnessError("Tools require object parameters and a handler", "invalid_plugin")
        env = requires_env if requires_env is not None else self._manifest.get("requires_env", [])
        if not isinstance(env, (list, tuple)) or any(not isinstance(key, str) for key in env):
            raise HarnessError("Invalid requires_env", "invalid_plugin")
        if type(is_async) is not bool or inspect.iscoroutinefunction(handler) != is_async:
            raise HarnessError("is_async must match the handler callable", "invalid_plugin")
        tool = Tool(name, self.plugin_id, toolset, function, handler, bool(is_async), check_fn, tuple(env))
        self._registry.add(tool)
        return tool
