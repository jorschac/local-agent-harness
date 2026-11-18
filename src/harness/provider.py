from __future__ import annotations
import asyncio
import os
from pathlib import Path
from .config import validate_model
from .errors import HarnessError
from .paths import path_map


class OpenAIProvider:
    """Lazy credential lookup: importing/starting/status never reads a model secret."""
    def __init__(self, home: Path, config: dict):
        self.home, self.config = home, dict(config)
        self.client = None
        self.deadline = min(float(config["run_timeout"]), 45.0)

    async def complete(self, messages: list, tools: list, max_tokens: int) -> dict:
        import httpx
        from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
        validate_model(self.config["model"])
        if self.client is None:
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                key_path = path_map(self.home)["key"]
                try:
                    key = key_path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    raise HarnessError("Provider credentials are not configured", "provider_not_configured", 503) from None
            if not key:
                raise HarnessError("Provider credentials are not configured", "provider_not_configured", 503)
            # Public HTTPX client only; no SDK-private transport access or retries.
            self.client = AsyncOpenAI(api_key=key, base_url=self.config["base_url"], max_retries=0, timeout=self.deadline, http_client=httpx.AsyncClient(timeout=self.deadline, trust_env=False))
        kwargs = {"model": self.config["model"], "messages": messages, "max_tokens": max_tokens, "n": 1, "stream": False, "extra_body": {"provider": {"require_parameters": True, "allow_fallbacks": False}}}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            # HTTPX timeouts are per-operation. This is also a whole-request deadline.
            response = await asyncio.wait_for(self.client.chat.completions.create(**kwargs), timeout=self.deadline)
            return response.model_dump(mode="json")
        except HarnessError:
            raise
        except (asyncio.TimeoutError, APITimeoutError):
            raise HarnessError("Provider request exceeded its deadline; no fallback was attempted", "provider_timeout", 504, error_type="api_error") from None
        except APIStatusError as exc:
            error_body = exc.body if isinstance(exc.body, dict) else {}
            detail = error_body.get("error", error_body)
            message_hint = str(detail.get("message", "")).lower() if isinstance(detail, dict) else ""
            if exc.status_code == 403 and ("not available in your region" in message_hint or "geo restriction" in message_hint):
                code, message = "provider_region_unavailable", "Configured model is unavailable in this region"
            elif exc.status_code in (401, 403):
                code, message = "provider_authentication_error", "Provider rejected credentials or access"
            elif exc.status_code == 429:
                code, message = "provider_rate_limit", "Provider rate limit exceeded"
            elif 400 <= exc.status_code < 500:
                code, message = "provider_request_rejected", "Provider rejected the configured request"
            else:
                code, message = "provider_unavailable", "Provider service is unavailable"
            raise HarnessError(message + "; no fallback was attempted", code, 502, error_type="api_error") from None
        except APIConnectionError:
            raise HarnessError("Provider connection failed; no fallback was attempted", "provider_connection_error", 502, error_type="api_error") from None
        except Exception:
            raise HarnessError("Provider request failed; no automatic fallback was attempted", "provider_error", 502, error_type="api_error") from None

    async def close(self):
        if self.client is not None:
            await self.client.close()
