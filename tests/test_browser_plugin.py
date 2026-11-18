"""No real Chrome, pages, network, user profiles or models are used here."""
from collections import defaultdict, deque
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "browser-safety"
spec = importlib.util.spec_from_file_location("browser_safety_under_test", PLUGIN_DIR / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
ENDPOINT = "ws://127.0.0.1:9222/devtools/browser/test-owned"


class FakeRuntime:
    def __init__(self):
        self.instance_id = "owned-instance"
        self.calls = 0
        self.error = None

    def ensure(self):
        self.calls += 1
        if self.error:
            raise self.error
        return {"instance_id": self.instance_id, "endpoint": ENDPOINT}


class FakeCDP:
    def __init__(self):
        self.targets = {"foreign": {"targetId": "foreign", "type": "page", "title": "foreign", "url": "about:blank"},
                        "worker": {"targetId": "worker", "type": "service_worker", "url": "https://example.org/sw.js"}}
        self.calls = []
        self.actions = defaultdict(deque)
        self.created = 0
        self.connections = 0
        self.exits = 0
        self.connection_error = None
        self.on_close = None

    def factory(self, endpoint):
        assert endpoint == ENDPOINT
        return self

    def __enter__(self):
        self.connections += 1
        if self.connection_error:
            raise self.connection_error
        return self

    def __exit__(self, *args):
        self.exits += 1

    def call(self, method, params=None, target_id=None):
        self.calls.append((method, params or {}, target_id))
        if self.actions[method]:
            action = self.actions[method].popleft()
            if isinstance(action, Exception):
                raise action
            if callable(action):
                return action(params or {}, target_id)
            return action
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        if method == "Target.createTarget":
            self.created += 1
            target = f"own-{self.created}"
            self.targets[target] = {"targetId": target, "type": "page", "url": params["url"], "title": "owned"}
            return {"targetId": target}
        if method == "Target.closeTarget":
            if self.on_close:
                self.on_close(params["targetId"])
            if params["targetId"] in self.targets:
                del self.targets[params["targetId"]]
                return {"success": True}
            return {"success": False}
        if method == "Page.navigate":
            self.targets[target_id]["url"] = params["url"]
            return {"frameId": "frame-1", "loaderId": "loader-1"}
        if method == "Runtime.enable":
            return {}
        if method == "Runtime.compileScript":
            return {"scriptId": "script-1"}
        if method == "Runtime.runScript":
            return {"result": {"type": "string", "value": "page result"}}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": {"title": "Visible page", "url": "https://example.org", "text": "x" * 8000, "text_length": 9500}}}
        raise AssertionError(f"Unexpected CDP command: {method}")

    def methods(self):
        return [call[0] for call in self.calls]


@pytest.fixture
def browser(tmp_path):
    runtime, cdp = FakeRuntime(), FakeCDP()
    service = plugin.BrowserService(tmp_path / "state", runtime=runtime, transport_factory=cdp.factory)
    return service, cdp, runtime


def ledger(service):
    return json.loads(service.ledger_path.read_text())


def test_open_three_and_reject_fourth_persistently(browser):
    service, cdp, runtime = browser
    ids = [service.open("alice")["target_id"] for _ in range(3)]
    assert len(set(ids)) == 3
    result = service.open("alice")
    assert result["error"]["code"] == "quota_exceeded"
    assert cdp.created == 3
    replacement = plugin.BrowserService(service.state_dir, runtime=runtime, transport_factory=cdp.factory)
    assert replacement.open("alice")["error"]["code"] == "quota_exceeded"
    assert replacement.open("bob")["quota_used"] == 1


@pytest.mark.parametrize("answer", [{"success": False}, {}, {"success": 1}, {"success": "true"}, TimeoutError("uncertain close"), RuntimeError("target vanished")])
def test_close_not_exact_true_retains_owned_and_quota(browser, answer):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    cdp.actions["Target.closeTarget"].append(answer)
    result = service.close("alice", target)
    assert result["closed"] is False and result["unconfirmed"] is True
    assert result["quota_used"] == 1
    saved = ledger(service)["sessions"]["alice"]["owned"][target]
    assert saved["state"] == "unconfirmed"
    assert service.status("alice")["quota_used"] == 1


def test_close_success_only_reclaims_and_retry_is_foreign(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    assert service.close("alice", target) == {"target_id": target, "closed": True, "quota_used": 0}
    assert service.close("alice", target)["error"]["code"] == "foreign_target"
    assert target not in ledger(service)["sessions"]["alice"]["owned"]


def test_missing_snapshot_never_reclaims_and_close_false_still_retains(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    del cdp.targets[target]
    status = service.status("alice")
    assert status["quota_used"] == 1
    assert status["owned"][0]["present_in_snapshot"] is False
    assert service.close("alice", target)["quota_used"] == 1


def test_close_can_work_even_when_get_targets_would_fail(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    cdp.actions["Target.getTargets"].append(TimeoutError("snapshot failure"))
    assert service.close("alice", target)["closed"] is True
    assert len(cdp.actions["Target.getTargets"]) == 1


@pytest.mark.parametrize("operation", ["navigate", "eval", "extract", "close"])
def test_foreign_and_other_session_rejected_without_commands(browser, operation):
    service, cdp, runtime = browser
    target = service.open("alice")["target_id"]
    before = (len(cdp.calls), runtime.calls)
    extras = {"navigate": ["https://example.org"], "eval": ["document.title"], "extract": [], "close": []}[operation]
    for target_id in (target, "foreign"):
        result = getattr(service, operation)("bob", target_id, *extras)
        assert result["error"]["code"] == "foreign_target"
    assert (len(cdp.calls), runtime.calls) == before


@pytest.mark.parametrize("answer", [TimeoutError("lost create acknowledgement"), {}, {"targetId": "foreign"}])
def test_uncertain_create_write_ahead_and_persistent_quota(browser, answer):
    service, cdp, runtime = browser
    if answer == {"targetId": "foreign"}:
        # Simulate a known claimed ID: never overwrite ownership.
        service.open("bob")
        answer = {"targetId": "own-1"}
    def response(params, target_id):
        pending = ledger(service)["sessions"]["alice"]["pending"]
        assert len(pending) == 1
        assert next(iter(pending.values()))["state"] == "creating"
        if isinstance(answer, Exception):
            raise answer
        return answer
    cdp.actions["Target.createTarget"].append(response)
    result = service.open("alice")
    assert result["error"]["code"] == "create_unconfirmed"
    assert result["quota_used"] == 1
    replacement = plugin.BrowserService(service.state_dir, runtime=runtime, transport_factory=cdp.factory)
    status = replacement.status("alice")
    assert status["pending"][0]["state"] == "uncertain"
    assert status["quota_used"] == 1
    released = replacement.release("alice")
    assert released["unreclaimed"][0]["operation_id"] == result["operation_id"]
    assert released["quota_used"] == 1


def test_release_stops_durably_first_only_closes_own_and_retry(browser):
    service, cdp, _ = browser
    good = service.open("alice")["target_id"]
    unconfirmed = service.open("alice")["target_id"]
    other = service.open("bob")["target_id"]
    def verify_stopping(target):
        assert ledger(service)["sessions"]["alice"]["status"] == "stopping"
    cdp.on_close = verify_stopping
    cdp.actions["Target.closeTarget"].append(lambda p, _: (verify_stopping(p["targetId"]), cdp.targets.pop(p["targetId"]), {"success": True})[-1])
    cdp.actions["Target.closeTarget"].append({"success": False})
    result = service.release("alice")
    assert result["closed"] == [good]
    assert [item["target_id"] for item in result["unreclaimed"]] == [unconfirmed]
    assert cdp.targets[other] and cdp.targets["foreign"]
    for method, args in [("open", []), ("navigate", [unconfirmed, "https://example.org"]), ("eval", [unconfirmed, "document.title"]), ("extract", [unconfirmed])]:
        assert getattr(service, method)("alice", *args)["error"]["code"] == "session_stopping"
    assert service.status("alice")["session_status"] == "stopping"
    retried = service.release("alice")
    assert retried["closed"] == [unconfirmed]
    assert retried["unreclaimed"] == []
    assert "Browser.close" not in cdp.methods()


def test_release_connection_failure_retains_pages_and_stopping(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    cdp.connection_error = TimeoutError("connection unavailable")
    result = service.release("alice")
    assert result["session_status"] == "stopping"
    assert result["unreclaimed"][0]["target_id"] == target
    assert result["unreclaimed"][0]["state"] == "unconfirmed"
    assert result["quota_used"] == 1
    cdp.connection_error = None
    assert service.close("alice", target)["closed"] is True


def test_empty_release_does_not_launch_runtime(browser):
    service, cdp, runtime = browser
    assert service.release("alice")["unreclaimed"] == []
    assert runtime.calls == 0 and not cdp.calls
    assert service.open("alice")["error"]["code"] == "session_stopping"


def test_status_full_snapshot_annotates_all_and_popup_chain(browser):
    service, cdp, _ = browser
    root = service.open("alice")["target_id"]
    other = service.open("bob")["target_id"]
    cdp.targets.update({"popup-2": {"targetId": "popup-2", "type": "page", "openerId": "popup-1"},
                        "popup-1": {"targetId": "popup-1", "type": "page", "openerId": root},
                        "popup-3": {"targetId": "popup-3", "type": "page", "openerId": "popup-2"},
                        "noopener": {"targetId": "noopener", "type": "page"},
                        "foreign-child": {"targetId": "foreign-child", "type": "page", "openerId": "foreign"}})
    status = service.status("alice")
    assert len(status["targets"]) == len(cdp.targets)
    annotated = {row["targetId"]: row for row in status["targets"]}
    assert annotated[root]["ownership"] == "self"
    assert annotated[other]["ownership"] == "other_session"
    assert annotated["worker"]["ownership"] == "foreign"
    assert annotated["noopener"]["ownership"] == "foreign"
    assert annotated["foreign-child"]["ownership"] == "foreign"
    assert annotated["popup-3"]["ownership"] == "self"
    assert status["quota_used"] == 4 and status["quota_exceeded"] is True
    assert service.open("alice")["error"]["code"] == "quota_exceeded"
    result = service.release("alice")
    assert set(result["closed"]) == {root, "popup-1", "popup-2", "popup-3"}
    assert cdp.targets["noopener"] and cdp.targets["foreign-child"] and cdp.targets[other]


def test_open_checks_popup_snapshot_before_create(browser):
    service, cdp, _ = browser
    root = service.open("alice")["target_id"]
    for index in (1, 2):
        cdp.targets[f"popup-{index}"] = {"targetId": f"popup-{index}", "type": "page", "openerId": root}
    assert service.open("alice")["error"]["code"] == "quota_exceeded"
    assert cdp.created == 1


def test_delayed_popup_after_confirmed_close_keeps_provenance(browser):
    service, cdp, _ = browser
    root = service.open("alice")["target_id"]
    assert service.close("alice", root)["closed"] is True
    cdp.targets["late-popup"] = {"targetId": "late-popup", "type": "page", "openerId": root}
    status = service.status("alice")
    assert status["quota_used"] == 1
    assert status["owned"][0]["target_id"] == "late-popup"
    assert service.release("alice")["closed"] == ["late-popup"]


def test_release_final_snapshot_preserves_late_popup_for_retry(browser):
    service, cdp, _ = browser
    root = service.open("alice")["target_id"]
    def popup_during_close(target):
        cdp.targets["late-popup"] = {"targetId": "late-popup", "type": "page", "openerId": target}
    cdp.on_close = popup_during_close
    result = service.release("alice")
    assert result["closed"] == [root]
    assert [item["target_id"] for item in result["unreclaimed"]] == ["late-popup"]
    cdp.on_close = None
    assert service.release("alice")["unreclaimed"] == []


def test_navigate_owned_page_without_creating(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    result = service.navigate("alice", target, "https://example.org/path")
    assert result["target_id"] == target
    assert cdp.created == 1
    assert cdp.calls[-1] == ("Page.navigate", {"url": "https://example.org/path"}, target)
    cdp.actions["Page.navigate"].append({"errorText": "net::ERR_FAILED"})
    assert service.navigate("alice", target, "https://example.org")["error"]["code"] == "navigation_error"


def test_eval_compile_then_run_page_attached_and_no_evaluate(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    cdp.calls.clear()
    result = service.eval("alice", target, "document.title")
    assert result["result"]["value"] == "page result"
    assert cdp.methods() == ["Target.getTargets", "Runtime.enable", "Runtime.compileScript", "Runtime.runScript", "Target.getTargets"]
    compiled = cdp.calls[2]
    assert compiled[1]["expression"] == "document.title"
    assert compiled[1]["persistScript"] is True and compiled[2] == target
    assert cdp.calls[3][1] == {"scriptId": "script-1", "returnByValue": True, "awaitPromise": True}
    assert "not a permissions sandbox" in result["boundary"]


def test_eval_syntax_error_records_details_does_not_run(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    detail = {"text": "Uncaught", "lineNumber": 0, "columnNumber": 7, "exception": {"description": "SyntaxError: unexpected token"}}
    cdp.actions["Runtime.compileScript"].append({"exceptionDetails": detail})
    result = service.eval("alice", target, "const =")
    assert result["error"] == {"code": "script_syntax_error", "message": "JavaScript compilation failed", "execution": "not_started"}
    assert result["exception_details"] == detail
    assert "Runtime.runScript" not in cdp.methods()


def test_eval_runtime_error_records_details_and_popup(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    detail = {"text": "Uncaught", "exception": {"description": "Error: deliberate"}}
    def throw_with_popup(params, target_id):
        cdp.targets["new-popup"] = {"targetId": "new-popup", "type": "page", "openerId": target_id}
        return {"result": {"type": "object", "subtype": "error", "description": "Error: deliberate"}, "exceptionDetails": detail}
    cdp.actions["Runtime.runScript"].append(throw_with_popup)
    result = service.eval("alice", target, "throw new Error('deliberate')")
    assert result["error"]["code"] == "script_runtime_error"
    assert result["exception_details"] == detail
    assert result["quota_used"] == 2


def test_extract_only_fixed_script_and_caps(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    for cap in (4000, 25, 8000):
        result = service.extract("alice", target, cap)
        assert result["title"] == "Visible page" and result["url"] == "https://example.org"
        assert len(result["text"]) == cap and result["truncated"] is True
        expression = cdp.calls[-1]
        assert expression == ("Runtime.evaluate", {"expression": plugin.EXTRACT_SCRIPT, "returnByValue": True, "awaitPromise": False}, target)
    for bad in (0, 8001, True, 4.5, "4000"):
        assert service.extract("alice", target, bad)["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:document.title", "data:text/html,test", "about:config", "https:", ""])
def test_direct_urls_restricted_without_claiming_network_sandbox(browser, url):
    service, cdp, _ = browser
    result = service.open("alice", url)
    assert result["error"]["code"] in {"invalid_url", "invalid_arguments"}
    assert cdp.created == 0


def test_previous_runtime_target_not_reclaimed_or_sent_to_new_browser(browser):
    service, cdp, runtime = browser
    target = service.open("alice")["target_id"]
    runtime.instance_id = "new-owned-instance"
    before = cdp.methods().count("Target.closeTarget")
    result = service.close("alice", target)
    assert result["closed"] is False and result["quota_used"] == 1
    assert cdp.methods().count("Target.closeTarget") == before
    assert service.status("alice")["owned"][0]["present_in_snapshot"] is False
    assert service.release("alice")["quota_used"] == 1


def test_multiple_service_instances_share_atomic_quota(browser):
    service, cdp, runtime = browser
    services = [plugin.BrowserService(service.state_dir, runtime=runtime, transport_factory=cdp.factory) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda current: current.open("alice"), services))
    assert sum("target_id" in result for result in results) == 3
    assert sum(result.get("error", {}).get("code") == "quota_exceeded" for result in results) == 5
    assert len(ledger(service)["sessions"]["alice"]["owned"]) == 3


def test_private_permissions_and_corrupt_state_refusal(browser):
    service, cdp, _ = browser
    service.open("alice")
    assert service.state_dir.stat().st_mode & 0o777 == 0o700
    assert service.ledger_path.stat().st_mode & 0o777 == 0o600
    assert (service.state_dir / "ledger.lock").stat().st_mode & 0o777 == 0o600
    service.ledger_path.write_text("{not valid JSON")
    assert service.open("alice")["error"]["code"] == "state_corrupt"
    assert cdp.created == 1


def fake_chrome_runtime(tmp_path, **overrides):
    executable = tmp_path / "Chrome-fake"
    executable.write_text("test fixture: never executed\n")
    launches = []
    def launch(command, **kwargs):
        launches.append((command, kwargs))
        profile = next(value.split("=", 1)[1] for value in command if value.startswith("--user-data-dir="))
        (Path(profile) / "DevToolsActivePort").write_text("9222\n/devtools/browser/test-owned\n")
        return SimpleNamespace(pid=123456789)
    options = {"launcher": launch, "probe": lambda record: "owned"}
    options.update(overrides)
    runtime = plugin.ChromeRuntime(tmp_path / "runtime", str(executable), **options)
    return runtime, launches, executable


def test_runtime_singleton_reused_across_instances_no_real_process(tmp_path):
    runtime, launches, executable = fake_chrome_runtime(tmp_path)
    first = runtime.ensure()
    second = runtime.ensure()
    replacement = plugin.ChromeRuntime(runtime.state_dir, str(executable), launcher=runtime.launcher, probe=runtime.probe)
    third = replacement.ensure()
    assert first["instance_id"] == second["instance_id"] == third["instance_id"]
    assert len(launches) == 1
    command = launches[0][0]
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=0" in command
    assert not any("headless" in argument for argument in command)
    assert f"--user-data-dir={runtime.profile}" in command
    assert runtime.descriptor.stat().st_mode & 0o777 == 0o600
    assert runtime.profile.stat().st_mode & 0o777 == 0o700
    assert (runtime.state_dir / "chrome.log").stat().st_mode & 0o777 == 0o600


def test_runtime_failed_spawn_retains_descriptor_and_refuses_duplicate(tmp_path):
    count = []
    def uncertain_launcher(*args, **kwargs):
        count.append(1)
        raise OSError("spawn outcome unknown")
    runtime, _, _ = fake_chrome_runtime(tmp_path, launcher=uncertain_launcher, probe=lambda record: "uncertain")
    with pytest.raises(plugin.BrowserFault) as first:
        runtime.ensure()
    assert first.value.code == "browser_spawn_uncertain"
    assert json.loads(runtime.descriptor.read_text())["status"] == "starting"
    with pytest.raises(plugin.BrowserFault) as second:
        runtime.ensure()
    assert second.value.code == "runtime_uncertain"
    assert count == [1]


def test_runtime_missing_descriptor_never_adopts_profile_endpoint(tmp_path):
    runtime, launches, _ = fake_chrome_runtime(tmp_path)
    runtime.profile.mkdir()
    (runtime.profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/test-owned\n")
    with pytest.raises(plugin.BrowserFault) as fault:
        runtime.ensure()
    assert fault.value.code == "runtime_uncertain" and launches == []


def test_runtime_endpoint_change_refused(tmp_path):
    runtime, launches, _ = fake_chrome_runtime(tmp_path)
    runtime.ensure()
    (runtime.profile / "DevToolsActivePort").write_text("9223\n/devtools/browser/foreign\n")
    with pytest.raises(plugin.BrowserFault) as fault:
        runtime.ensure()
    assert fault.value.code == "runtime_uncertain" and len(launches) == 1


def test_runtime_dead_pid_with_dangling_profile_lock_refused(tmp_path):
    runtime, launches, _ = fake_chrome_runtime(tmp_path)
    runtime.ensure()
    runtime.probe = lambda record: "dead"
    (runtime.profile / "SingletonLock").symlink_to("not-a-real-file")
    with pytest.raises(plugin.BrowserFault) as fault:
        runtime.ensure()
    assert fault.value.code == "profile_busy" and len(launches) == 1


def test_runtime_start_timeout_keeps_pid_and_no_second_launch(tmp_path):
    ticks = iter([0, 0, 1, 20, 20, 21, 40])
    def launch_without_endpoint(command, **kwargs):
        count.append(command)
        return SimpleNamespace(pid=123456789)
    count = []
    runtime, _, _ = fake_chrome_runtime(tmp_path, launcher=launch_without_endpoint,
                                       clock=lambda: next(ticks), sleeper=lambda _: None)
    for _ in range(2):
        with pytest.raises(plugin.BrowserFault) as fault:
            runtime.ensure()
        assert fault.value.code == "browser_start_timeout"
    assert len(count) == 1
    assert json.loads(runtime.descriptor.read_text())["pid"] == 123456789


@pytest.mark.parametrize("endpoint", ["ws://192.168.1.2:9222/devtools/browser/test", "ws://localhost:9222/devtools/browser/test", "wss://127.0.0.1:9222/devtools/browser/test", "ws://127.0.0.1:9222/devtools/page/test", "ws://user:pass@127.0.0.1:9222/devtools/browser/test"])
def test_transport_endpoint_restricts_exact_loopback(endpoint):
    with pytest.raises(plugin.BrowserFault) as fault:
        plugin.SyncCDPTransport(endpoint)
    assert fault.value.code == "unsafe_endpoint"


def test_sync_transport_flatten_attach_ignores_events_and_closes(monkeypatch):
    import websockets.sync.client
    packets, sent = deque(), []
    class FakeWebsocket:
        closed = False
        def send(self, text):
            message = json.loads(text)
            sent.append(message)
            packets.append(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))
            result = {"sessionId": "attached-1"} if message["method"] == "Target.attachToTarget" else {"frameId": "frame"}
            packets.append(json.dumps({"id": message["id"], "result": result}))
        def recv(self, timeout=None):
            assert timeout > 0
            return packets.popleft()
        def close(self):
            self.closed = True
    socket = FakeWebsocket()
    options = []
    def connect(endpoint, **kwargs):
        assert endpoint == ENDPOINT
        options.append(kwargs)
        return socket
    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    with plugin.SyncCDPTransport(ENDPOINT) as transport:
        assert transport.call("Page.navigate", {"url": "https://example.org"}, "own-1") == {"frameId": "frame"}
        transport.call("Runtime.enable", {}, "own-1")
    assert sent[0]["params"] == {"targetId": "own-1", "flatten": True}
    assert sent[1]["sessionId"] == sent[2]["sessionId"] == "attached-1"
    assert sum(message["method"] == "Target.attachToTarget" for message in sent) == 1
    assert options[0]["proxy"] is None and socket.closed


def test_manifest_registration_and_harness_home_without_browser(tmp_path, monkeypatch):
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
    class Context:
        plugin_id = "browser-safety"
        def __init__(self, config=None):
            self.settings = config or {}
            self.tools = {}
        def get_config(self, key, default=None):
            return self.settings.get(key, default)
        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs
    context = Context()
    service = plugin.register(context)
    assert service.state_dir == tmp_path / "home" / "plugin-data" / "browser-safety"
    assert not service.runtime.descriptor.exists()
    assert manifest["kind"] == "standalone" and manifest["manifest_version"] == 2
    assert set(context.tools) == set(manifest["provides_tools"])
    for name, tool in context.tools.items():
        assert name.startswith("browser_") and tool["is_async"] is False
        assert tool["schema"]["name"] == name
        assert tool["schema"]["parameters"]["additionalProperties"] is False
    result = json.loads(context.tools["browser_release"]["handler"]({}, session_id="s", task_id="t"))
    assert result["session_id"] == "s" and result["unreclaimed"] == []
    override = Context({"state_dir": str(tmp_path / "override"), "quota": 2})
    assert plugin.register(override).state_dir == tmp_path / "override"


def test_register_handler_default_direct_session_and_json_errors(tmp_path):
    class Context:
        plugin_id = "browser-safety"
        tools = {}
        def get_config(self, key, default=None):
            return str(tmp_path) if key == "state_dir" else default
        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs
    context = Context()
    plugin.register(context)
    released = json.loads(context.tools["browser_release"]["handler"]({}))
    assert released["session_id"] == "local-direct"
    invalid = json.loads(context.tools["browser_open"]["handler"]({"unexpected": 1}))
    assert invalid["error"]["code"] == "invalid_arguments"


def test_create_ack_for_preexisting_foreign_is_never_claimed(browser):
    service, cdp, _ = browser
    cdp.actions["Target.createTarget"].append({"targetId": "foreign"})
    result = service.open("alice")
    assert result["error"]["code"] == "create_unconfirmed"
    status = service.status("alice")
    assert status["owned"] == [] and status["quota_used"] == 1
    foreign = next(target for target in status["targets"] if target["targetId"] == "foreign")
    assert foreign["ownership"] == "foreign"
    service.release("alice")
    assert "Target.closeTarget" not in cdp.methods()
    assert "foreign" in cdp.targets


def test_release_retry_discovers_popup_after_last_confirmed_close(browser):
    service, cdp, _ = browser
    root = service.open("alice")["target_id"]
    assert service.release("alice")["unreclaimed"] == []
    cdp.targets["delayed-popup"] = {"targetId": "delayed-popup", "type": "page", "openerId": root}
    result = service.release("alice")
    assert result["closed"] == ["delayed-popup"]
    assert result["unreclaimed"] == [] and result["quota_used"] == 0
    assert "foreign" in cdp.targets


def test_close_persists_closing_before_dispatch(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    def uncertain(params, target_id):
        assert ledger(service)["sessions"]["alice"]["owned"][params["targetId"]]["state"] == "closing"
        raise TimeoutError("ack dropped")
    cdp.actions["Target.closeTarget"].append(uncertain)
    assert service.close("alice", target)["quota_used"] == 1
    assert ledger(service)["sessions"]["alice"]["owned"][target]["state"] == "unconfirmed"
    assert service.close("alice", target)["closed"] is True


def test_uncertain_creates_exhaust_quota_without_adopting_unknown_target(browser):
    service, cdp, _ = browser
    for _ in range(3):
        cdp.actions["Target.createTarget"].append(TimeoutError("unknown created page"))
        assert service.open("alice")["error"]["code"] == "create_unconfirmed"
    cdp.targets["unknown"] = {"targetId": "unknown", "type": "page"}
    assert service.open("alice")["error"]["code"] == "quota_exceeded"
    status = service.status("alice")
    assert status["quota_used"] == 3 and len(status["pending"]) == 3
    assert status["owned"] == []
    assert service.release("alice")["quota_used"] == 3
    assert "unknown" in cdp.targets and "Target.closeTarget" not in cdp.methods()


def test_snapshot_failure_release_still_closes_known_owned_targets(browser):
    service, cdp, _ = browser
    target = service.open("alice")["target_id"]
    cdp.actions["Target.getTargets"].append(TimeoutError("snapshot unavailable"))
    result = service.release("alice")
    assert result["closed"] == [target]
    assert result["unreclaimed"] == []
    assert result["failures"][0]["phase"] == "snapshot"


def test_runtime_singleton_lock_serializes_concurrent_instances(tmp_path):
    runtime, launches, executable = fake_chrome_runtime(tmp_path)
    runtimes = [plugin.ChromeRuntime(runtime.state_dir, str(executable), launcher=runtime.launcher, probe=runtime.probe) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda current: current.ensure(), runtimes))
    assert len(launches) == 1
    assert len({record["instance_id"] for record in records}) == 1


def test_runtime_mismatched_descriptor_configuration_refused(tmp_path):
    runtime, launches, _ = fake_chrome_runtime(tmp_path)
    runtime.ensure()
    saved = json.loads(runtime.descriptor.read_text())
    saved["profile"] = str(tmp_path / "foreign-profile")
    runtime.descriptor.write_text(json.dumps(saved))
    with pytest.raises(plugin.BrowserFault) as fault:
        runtime.ensure()
    assert fault.value.code == "runtime_mismatch" and len(launches) == 1


def test_browser_error_results_do_not_expose_python_eval_or_host_imports():
    source = (PLUGIN_DIR / "__init__.py").read_text()
    assert "import harness" not in source and "from harness" not in source
    assert "eval(" not in source.replace("def eval(", "def script_tool(").replace("def _eval(", "def _script_tool(")
    assert "exec(" not in source
    assert "Runtime.compileScript" in source and "Runtime.runScript" in source
