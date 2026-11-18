from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Message(StrictBody):
    role: Literal["system", "developer", "user", "assistant"]
    content: StrictStr


class StreamOptions(StrictBody):
    include_usage: StrictBool = False


class ChatRequest(StrictBody):
    model: StrictStr
    messages: list[Message] = Field(min_length=1)
    stream: StrictBool = False
    n: StrictInt = 1
    max_tokens: StrictInt | None = Field(default=None, ge=1)
    stream_options: StreamOptions | None = None
    harness_plugins: list[StrictStr] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.n != 1:
            raise ValueError("Only n=1 is supported")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if self.harness_plugins is not None and len(set(self.harness_plugins)) != len(self.harness_plugins):
            raise ValueError("Duplicate plugins are not allowed")
        return self


class ToolCallRequest(StrictBody):
    name: StrictStr
    arguments: dict[str, Any]
    harness_plugins: list[StrictStr] | None = None


class ConfigPatch(StrictBody):
    path: StrictStr
    value: Any = None
    unset: StrictBool = False
    expected_revision: StrictInt | None = None


class PluginAdd(StrictBody):
    path: StrictStr
    trust: StrictBool = False
    replace: StrictBool = False


class PluginEnable(StrictBody):
    trust: StrictBool = False


class PluginSettings(StrictBody):
    key: StrictStr
    value: Any = None
    unset: StrictBool = False


class SessionCreate(StrictBody):
    title: StrictStr | None = None
