"""Authenticated loopback HTTP facade; never imports the CLI or daemon module."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import fcntl
import inspect
import json
import os
from pathlib import Path
import secrets
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException
from .api_models import ChatRequest, ConfigPatch, PluginAdd, PluginEnable, PluginSettings, SessionCreate, ToolCallRequest
from .config import validate_config
from .errors import HarnessError, safe_error
from .management import dispatch
from .paths import init_home, load_config, private_write
from .registry import parse_json
from .runs import RunManager
from .store import Store, TERMINAL


def create_app(home: Path | str | None = None, provider=None) -> FastAPI:
    paths = init_home(Path(home) if home is not None else None)
    root = paths["home"]
    token = paths["token"].read_text(encoding="utf-8").strip()
    if len(token) < 32 or len(set(token)) < 8 or any(character.isspace() for character in token):
        raise HarnessError("Local authentication token must be a strong token of at least 32 characters", "invalid_local_token", 500)
    instance_id = uuid.uuid4().hex

    @asynccontextmanager
    async def lifespan(app):
        fd = os.open(paths["owner_lock"], os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise HarnessError("Another owner holds this home", "owner_conflict", 409) from None
        store = None
        descriptor_written = False
        try:
            config = load_config(root)
            validate_config(config)
            store = Store(paths["database"])
            store.recover()
            manager = RunManager(root, store, provider)
            app.state.store, app.state.runs = store, manager
            descriptor = {"instance_id": instance_id, "pid": os.getpid(), "home": str(root), "host": config["server"]["host"], "port": config["server"]["port"], "started_at": time.time(), "ready": True}
            private_write(paths["owner_descriptor"], json.dumps(descriptor) + "\n")
            descriptor_written = True
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            if hasattr(app.state, "runs"):
                await app.state.runs.drain()
            if app.state.shutdown_task is not None:
                await asyncio.shield(app.state.shutdown_task)
            if store is not None:
                store.close()
            if descriptor_written:
                # Owner lock prevents another process replacing the record here.
                paths["owner_descriptor"].unlink(missing_ok=True)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    app = FastAPI(title="Local Harness", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home = root
    app.state.paths = paths
    app.state.instance_id = instance_id
    app.state.ready = False
    app.state.request_shutdown = None
    app.state.shutdown_task = None

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        # CLI/SDK requests do not send Origin. Reject all browser-origin requests,
        # including Origin:null, rather than exposing a loopback bearer surface.
        if "origin" in request.headers:
            return JSONResponse(HarnessError("Browser-origin requests are not permitted", "origin_forbidden", 403).body(), status_code=403)
        if request.url.path != "/health":
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer ") or not secrets.compare_digest(header[7:], token):
                return JSONResponse(HarnessError("A valid local bearer token is required", "unauthorized", 401, error_type="authentication_error").body(), status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            if request.method in ("POST", "PUT", "PATCH"):
                raw_body = await request.body()
                if raw_body:
                    if len(raw_body) > 1_048_576:
                        raise HarnessError("Request exceeds the 1 MiB limit", "request_too_large", 413)
                    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise HarnessError("Request body must use application/json", param="Content-Type")
                    try:
                        parse_json(raw_body.decode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise HarnessError("Request body must be strict JSON with unique keys and finite numbers", "invalid_json", 400) from None
            return await call_next(request)
        except Exception as exc:
            return JSONResponse(safe_error(exc), status_code=exc.status if isinstance(exc, HarnessError) else 500)

    @app.exception_handler(HarnessError)
    async def domain_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = exc.errors()
        loc = errors[0].get("loc", ()) if errors else ()
        param = ".".join(str(part) for part in loc if part != "body")
        return JSONResponse(HarnessError("Unsupported or invalid request field", "invalid_request", 400, param=param or None).body(), status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(HarnessError("Route or resource unavailable", "not_found" if exc.status_code == 404 else "http_error", exc.status_code).body(), status_code=exc.status_code)

    def manager() -> RunManager:
        if not app.state.ready:
            raise HarnessError("Owner is not ready", "not_ready", 503)
        return app.state.runs

    def mutate(action, payload):
        manager().require_idle()
        return dispatch(root, action, payload)

    def snapshot(rid):
        run = manager().store.run(rid)
        run["events"] = manager().store.events(rid)
        return run

    def accepted(run):
        return {key: run[key] for key in ("run_id", "session_id", "kind", "status", "events_url", "status_url")}

    def headers(run):
        return {"X-Harness-Run-Id": run["run_id"], "X-Harness-Session-Id": run["session_id"]}

    @app.get("/health")
    async def health():
        return {"status": "ok", "ready": app.state.ready}

    @app.get("/api/status")
    async def status():
        runs = manager()
        return {"instance_id": instance_id, "pid": os.getpid(), "ready": app.state.ready and not runs.draining, "busy": runs.busy, "draining": runs.draining, "config_revision": load_config(root)["revision"], "active_run": runs.active.id if runs.active else None}

    @app.get("/v1/models")
    async def models():
        manager()
        config = load_config(root)
        return {"object": "list", "data": [{"id": config["alias"], "object": "model", "created": 0, "owned_by": "local-harness"}]}

    @app.post("/api/runs", status_code=202)
    async def submit_run(body: ChatRequest, request: Request):
        run = manager().submit(body.model_dump(exclude_none=True), session_id=request.headers.get("x-harness-session-id"), idempotency_key=request.headers.get("idempotency-key"))
        return JSONResponse(accepted(run), status_code=202, headers=headers(run))

    @app.get("/api/runs")
    async def list_runs():
        return manager().store.runs()

    @app.get("/api/runs/{rid}")
    async def get_run(rid: str):
        return snapshot(rid)

    @app.post("/api/runs/{rid}/cancel")
    async def cancel_run(rid: str):
        return manager().cancel(rid)

    @app.get("/api/runs/{rid}/events")
    async def run_events(rid: str, request: Request, after: int = 0):
        runs = manager()
        runs.store.run(rid)
        cursor = after
        value = request.headers.get("last-event-id")
        if value is not None:
            try:
                cursor = max(cursor, int(value))
            except ValueError:
                raise HarnessError("Last-Event-ID must be an integer", param="Last-Event-ID") from None
        if cursor < 0:
            raise HarnessError("Event cursor must be nonnegative", param="after")
        async def generate():
            nonlocal cursor
            last_heartbeat = time.monotonic()
            while True:
                for event in runs.store.events(rid, cursor):
                    cursor = event["id"]
                    data = {"id": cursor, "run_id": rid, "type": event["type"], **event["data"]}
                    yield "id: " + str(cursor) + "\nevent: " + event["type"] + "\ndata: " + json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n\n"
                if runs.store.run(rid)["status"] in TERMINAL:
                    yield "data: [DONE]\n\n"
                    return
                if await request.is_disconnected():
                    return  # Disconnect does not cancel the durable run.
                if time.monotonic() - last_heartbeat > 10:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.05)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers(runs.store.run(rid))})

    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, request: Request):
        runs = manager()
        run = runs.submit(body.model_dump(exclude_none=True), session_id=request.headers.get("x-harness-session-id"), idempotency_key=request.headers.get("idempotency-key"))
        response_headers = headers(run)
        if body.stream:
            async def final_text_sse():
                rid = run["run_id"]
                base = {"id": "chatcmpl-" + rid, "created": int(run["created_at"]), "model": body.model, "object": "chat.completion.chunk"}
                def chunk(delta, finish=None):
                    return "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"
                # This is acceptance streaming, not upstream token streaming. The first
                # role chunk is sent before waiting for registration/model/tool work.
                yield chunk({"role": "assistant", "content": ""})
                last_heartbeat = time.monotonic()
                while runs.store.run(rid)["status"] not in TERMINAL:
                    if await request.is_disconnected():
                        return  # Disconnect never cancels the durable run.
                    if time.monotonic() - last_heartbeat >= 10:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.monotonic()
                    await asyncio.sleep(0.05)
                terminal = await runs.wait(rid)
                if terminal["status"] not in ("completed", "incomplete"):
                    error = terminal.get("error") or HarnessError("Run did not complete", "run_failed", 500).body()
                    # OpenAI SDK interprets this real JSON error as APIError. Never
                    # manufacture a successful finish_reason after HTTP 200.
                    yield "data: " + json.dumps(error, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                result = terminal["result"]
                text = result["choices"][0]["message"]["content"]
                for offset in range(0, len(text), 128):
                    yield chunk({"content": text[offset:offset + 128]})
                yield chunk({}, result["choices"][0]["finish_reason"])
                if body.stream_options is not None and body.stream_options.include_usage:
                    yield "data: " + json.dumps({**base, "choices": [], "usage": result["usage"]}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(final_text_sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **response_headers})
        run = await runs.wait(run["run_id"])
        if run["status"] not in ("completed", "incomplete"):
            error = run.get("error") or HarnessError("Run did not complete", "run_failed", 500).body()
            code = error["error"]["code"]
            status_code = 408 if code == "run_timeout" else 409 if code == "run_cancelled" else 422 if code in ("tool_limit", "round_limit") else 503 if code == "provider_not_configured" else 502
            return JSONResponse(error, status_code=status_code, headers=response_headers)
        return JSONResponse(run["result"], headers=response_headers)

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: SessionCreate):
        runs = manager()
        runs.require_idle()
        session = runs.store.create_session(body.title)
        return {**session, "session_id": session["id"]}

    @app.get("/api/sessions")
    async def list_sessions():
        return manager().store.sessions()

    @app.get("/api/sessions/{sid}")
    async def get_session(sid: str):
        return manager().store.session(sid)

    @app.get("/api/sessions/{sid}/messages")
    async def get_messages(sid: str):
        return manager().store.messages(sid)

    @app.post("/api/sessions/{sid}/close")
    async def close_session(sid: str):
        runs = manager()
        runs.require_idle()
        return runs.store.close_session(sid)

    @app.get("/api/plugins/list")
    @app.get("/api/plugins")
    async def list_plugins():
        manager()
        return dispatch(root, "plugins.list", {})

    @app.get("/api/plugins/{pid}")
    async def get_plugin(pid: str):
        manager()
        return dispatch(root, "plugins.show", {"id": pid})

    @app.post("/api/plugins", status_code=201)
    async def add_plugin(body: PluginAdd):
        return mutate("plugins.add", body.model_dump())

    @app.post("/api/plugins/{pid}/enable")
    async def enable_plugin(pid: str, body: PluginEnable):
        return mutate("plugins.enable", {"id": pid, **body.model_dump()})

    @app.post("/api/plugins/{pid}/disable")
    async def disable_plugin(pid: str):
        return mutate("plugins.disable", {"id": pid})

    @app.put("/api/plugins/{pid}/settings")
    async def plugin_settings(pid: str, body: PluginSettings):
        return mutate("plugins.settings.unset" if body.unset else "plugins.settings.set", {"id": pid, **body.model_dump()})

    @app.get("/api/tools")
    async def list_tools(harness_plugins: str | None = None):
        manager()
        selected = None
        if harness_plugins is not None:
            try:
                selected = parse_json(harness_plugins)
            except (ValueError, TypeError):
                raise HarnessError("harness_plugins must be a JSON array", param="harness_plugins") from None
            if not isinstance(selected, list) or any(not isinstance(pid, str) for pid in selected):
                raise HarnessError("harness_plugins must be a string array", param="harness_plugins")
        return manager().inspect_tools(selected)

    @app.post("/api/tools/call", status_code=202)
    async def tool_call(body: ToolCallRequest, request: Request):
        run = manager().submit(body.model_dump(exclude_none=True), kind="tool", idempotency_key=request.headers.get("idempotency-key"))
        return JSONResponse(accepted(run), status_code=202, headers=headers(run))

    @app.get("/api/config")
    async def get_config():
        manager()
        return dispatch(root, "config.get", {})

    @app.patch("/api/config")
    async def set_config(body: ConfigPatch):
        if not body.unset and "value" not in body.model_fields_set:
            raise HarnessError("value is required for config.set", param="value")
        return mutate("config.unset" if body.unset else "config.set", body.model_dump())

    @app.post("/api/shutdown")
    async def shutdown(request: Request):
        runs = manager()
        claimed = request.headers.get("x-harness-instance-id")
        if claimed is not None and claimed != instance_id:
            raise HarnessError("Instance identity does not match", "instance_conflict", 409)
        runs.draining = True
        if app.state.shutdown_task is None:
            async def request_exit():
                await runs.drain()
                callback = app.state.request_shutdown
                if callback is not None:
                    result = callback()
                    if inspect.isawaitable(result):
                        await result
            app.state.shutdown_task = asyncio.create_task(request_exit())
        return {"status": "draining", "instance_id": instance_id, "busy": runs.busy}

    return app


def main():
    import uvicorn
    paths = init_home()
    config = load_config(paths["home"])
    validate_config(config)
    app = create_app(paths["home"])
    server = uvicorn.Server(uvicorn.Config(app, host=config["server"]["host"], port=config["server"]["port"], access_log=False, log_level="warning"))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
