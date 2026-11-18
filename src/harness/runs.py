from __future__ import annotations
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from .config import validate_config
from .errors import HarnessError, safe_error
from .loop import AgentLoop, StopRequested
from .messages import validate_messages
from .paths import load_config
from .plugins import PluginManager
from .provider import OpenAIProvider
from .store import Store, TERMINAL, new_id


@dataclass
class ActiveRun:
    id: str
    stop: asyncio.Event
    task: asyncio.Task | None = None
    reason: str | None = None


class RunManager:
    def __init__(self, home: Path, store: Store, provider=None):
        self.home, self.store, self.provider = Path(home), store, provider
        self.plugins = PluginManager(home)
        self.active: ActiveRun | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.draining = False
        self.tool_views = {}

    @property
    def busy(self) -> bool:
        return self.active is not None

    def require_idle(self):
        if self.draining:
            raise HarnessError("Server is draining", "server_draining", 503)
        if self.busy:
            raise HarnessError("The single execution slot is occupied", "harness_busy", 409)

    def inspect_tools(self, selected=None):
        """Read metadata/cached schemas only; inspection never imports plugin Python."""
        config = load_config(self.home)
        installed = config["plugins"]
        ids = [pid for pid, entry in installed.items() if entry.get("enabled")] if selected is None else list(selected)
        if len(set(ids)) != len(ids):
            raise HarnessError("Duplicate plugin selection", "invalid_plugin_selection")
        result = []
        for pid in ids:
            info = self.plugins.show(pid, config)
            if not info.get("trusted"):
                raise HarnessError("Explicit full-trust consent is required", "plugin_untrusted", 403)
            cached = self.tool_views.get((config["revision"], pid))
            if cached is not None:
                result.extend(cached)
            else:
                result.extend({"name": name, "plugin_id": pid, "loaded": False, "schema": None} for name in info.get("manifest", {}).get("provides_tools", []))
        return result

    def submit(self, body: dict, *, kind="chat", session_id: str | None = None, idempotency_key: str | None = None) -> dict:
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 256):
            raise HarnessError("Invalid Idempotency-Key", param="Idempotency-Key")
        fingerprint = hashlib.sha256(json.dumps({"kind": kind, "session_id": session_id, "body": body}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if idempotency_key:
            previous = self.store.idempotent(idempotency_key, fingerprint)
            if previous is not None:
                return previous
        self.require_idle()
        config = load_config(self.home)
        validate_config(config)
        input_messages, history = [], []
        if kind == "chat":
            if body["model"] != config["alias"]:
                raise HarnessError("Unknown local model; automatic fallback is disabled", "model_not_found", 404, "model")
            max_tokens = body.get("max_tokens") or config["output_tokens"]
            if max_tokens > config["output_tokens"]:
                raise HarnessError("max_tokens exceeds the configured output cap", "token_limit", param="max_tokens")
            input_messages = validate_messages(body["messages"], session=session_id is not None)
            if session_id is not None:
                session = self.store.session(session_id)
                if session["status"] != "open":
                    raise HarnessError("Session is closed or cannot safely continue", "session_closed", 409)
                history = self.store.messages(session_id)
            else:
                session_id = self.store.create_session()["id"]
        else:
            max_tokens = config["output_tokens"]
            session_id = "local-direct"
        rid = new_id("run")
        self.store.create_run(rid, kind, session_id, body, fingerprint, idempotency_key)
        active = ActiveRun(rid, asyncio.Event())
        self.active = active
        active.task = asyncio.create_task(self._supervise(active, kind, session_id, input_messages, history, body, config, max_tokens), name=rid)
        self.tasks[rid] = active.task
        return self.store.run(rid)

    async def _supervise(self, active, kind, sid, incoming, history, body, config, max_tokens):
        worker = asyncio.create_task(self._execute(active, kind, sid, incoming, history, body, config, max_tokens))
        stopper = asyncio.create_task(active.stop.wait())
        try:
            done, _ = await asyncio.wait({worker, stopper}, timeout=config["run_timeout"], return_when=asyncio.FIRST_COMPLETED)
            if worker not in done:
                if active.reason is None:
                    active.reason = "timeout"
                    active.stop.set()
                pending = "timing_out" if active.reason == "timeout" else "cancelling"
                self.store.update_run(active.id, status=pending, cancel_requested=True)
                self.store.event(active.id, "run." + pending, {"status": pending, "execution": "draining", "slot_retained": True})
            # Deliberately no wait_for/cancel: a sync handler is not cancellable.
            await asyncio.shield(worker)
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            # If infrastructure itself is cancelled, wait for the real operation first.
            if not worker.done():
                await asyncio.shield(worker)
            if self.active is active:
                self.active = None
            self.tasks.pop(active.id, None)

    async def _execute(self, active, kind, sid, incoming, history, body, config, max_tokens):
        self.store.update_run(active.id, status="running", started_at=time.time())
        self.store.event(active.id, "run.started", {"status": "running", "kind": kind})
        provider, loop = None, None
        try:
            # Registration/import can execute trusted synchronous code. It happens only
            # after admission, off the event loop, within the supervised run deadline.
            # A timed-out registration thread still owns the slot until it really drains.
            registry = await asyncio.to_thread(self.plugins.build_registry, body.get("harness_plugins"), config)
            view = registry.list()
            for pid in {tool["plugin_id"] for tool in view}:
                self.tool_views[(config["revision"], pid)] = [{**tool, "loaded": True} for tool in view if tool["plugin_id"] == pid]
            if active.stop.is_set():
                raise StopRequested()
            provider = self.provider if self.provider is not None else OpenAIProvider(self.home, config)
            user_task = incoming[-1]["content"] if incoming else json.dumps(body["arguments"], ensure_ascii=False)
            loop = AgentLoop(provider, registry, self.store, active.id, sid, config, active.stop, user_task)
            if kind == "chat":
                self.store.commit_messages(sid, active.id, incoming)
                result, generated = await loop.run(history + incoming, max_tokens)
                loop.checkpoint()
                self.store.complete_chat(sid, active.id, [], result, persisted=True)
            else:
                output = await loop.tool(body["name"], body["arguments"], "direct-" + active.id)
                result = {"kind": "tool", "name": body["name"], "result": json.loads(output)}
                self.store.update_run(active.id, status="completed", result=result, finished_at=time.time())
            status = self.store.run(active.id)["status"]
            self.store.event(active.id, "run." + status, {"status": status, "result": result, "tool_calls": loop.calls, "cost": loop.cost})
        except StopRequested:
            status = "timed_out" if active.reason == "timeout" else "cancelled"
            error = HarnessError("Run timed out after draining in-flight execution" if status == "timed_out" else "Run cancelled after draining in-flight execution", "run_timeout" if status == "timed_out" else "run_cancelled", 408 if status == "timed_out" else 409).body()
            self.store.update_run(active.id, status=status, error=error, finished_at=time.time())
            self.store.event(active.id, "run." + status, {"status": status, **error, "tool_calls": loop.calls if loop else 0})
        except Exception as exc:
            error = safe_error(exc)
            self.store.update_run(active.id, status="failed", error=error, finished_at=time.time())
            self.store.event(active.id, "run.failed", {"status": "failed", **error, "tool_calls": loop.calls if loop else 0})
        finally:
            if loop is not None:
                self.store.update_run(active.id, cost=loop.cost)
            if kind == "chat":
                self.store.block_if_unpaired(sid)
            if self.provider is None and provider is not None:
                await provider.close()

    async def wait(self, rid: str) -> dict:
        self.store.run(rid)
        task = self.tasks.get(rid)
        if task is not None:
            await asyncio.shield(task)
        return self.store.run(rid)

    def cancel(self, rid: str) -> dict:
        run = self.store.run(rid)
        if run["status"] in TERMINAL:
            return run
        if self.active is None or self.active.id != rid:
            raise HarnessError("Run is not owned by this process", "run_not_active", 409)
        if self.active.reason is None:
            self.active.reason = "cancel"
        self.active.stop.set()
        self.store.update_run(rid, cancel_requested=True)
        self.store.event(rid, "run.cancel_requested", {"slot_retained": True})
        return self.store.run(rid)

    async def drain(self):
        self.draining = True
        if self.active is not None:
            active = self.active
            self.cancel(active.id)
            if active.task is not None:
                await asyncio.shield(active.task)
