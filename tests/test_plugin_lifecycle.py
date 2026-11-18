import json
from pathlib import Path

import pytest
import yaml

from harness.daemon import offline_dispatch
from harness.errors import HarnessError
from harness.management import dispatch
from harness.paths import init_home, load_config
from harness.plugins import PluginManager
from harness.registry import PluginContext, Registry


def source_plugin(tmp_path, marker, name="lifecycle"):
    source = tmp_path / (name + "-source")
    source.mkdir()
    (source / "plugin.yaml").write_text(yaml.safe_dump({
        "name": name, "version": "1.0", "description": "Lifecycle fixture",
        "manifest_version": 2, "kind": "standalone", "provides_tools": [name + "_read"],
        "provides_hooks": [], "requires_env": [], "config_schema": {}}))
    (source / "__init__.py").write_text(f'''
from pathlib import Path
Path({str(marker)!r}).write_text("loaded")
def register(ctx):
    def read(args): return 'plain text'
    ctx.register_tool({name + '_read'!r}, {name!r}, {{"name":{name + '_read'!r},"description":"Read", "parameters":{{"type":"object", "additionalProperties":False}}}}, read)
''')
    return source


def test_add_and_offline_enable_never_execute_code(tmp_path):
    home = tmp_path / "home"
    init_home(home)
    marker = tmp_path / "imported"
    source = source_plugin(tmp_path, marker)
    added = offline_dispatch(home, "plugins.add", {"path": str(source), "trust": True})
    assert not marker.exists()
    assert added["enabled"] is False
    installed = Path(added["path"])
    assert installed == home / "plugins/lifecycle"
    assert installed != source
    offline_dispatch(home, "plugins.enable", {"id": "lifecycle", "trust": True})
    assert not marker.exists()
    registry = PluginManager(home).build_registry()
    assert marker.read_text() == "loaded"
    assert list(registry.tools) == ["lifecycle_read"]


def test_source_independent_but_installed_mutation_rejected(tmp_path):
    home = tmp_path / "home"; init_home(home)
    source = source_plugin(tmp_path, tmp_path / "marker")
    added = dispatch(home, "plugins.add", {"path": str(source), "trust": True})
    (source / "__init__.py").write_text("raise RuntimeError('source changed')")
    PluginManager(home).build_registry(["lifecycle"])
    Path(added["path"]).joinpath("__init__.py").write_text("raise RuntimeError('installed changed')")
    with pytest.raises(HarnessError, match="changed"):
        PluginManager(home).build_registry(["lifecycle"])


def test_symlink_rejected_and_replacement_disabled(tmp_path):
    home = tmp_path / "home"; init_home(home)
    source = source_plugin(tmp_path, tmp_path / "marker")
    (source / "link").symlink_to(tmp_path / "marker")
    with pytest.raises(HarnessError):
        dispatch(home, "plugins.add", {"path": str(source), "trust": True})
    (source / "link").unlink()
    dispatch(home, "plugins.add", {"path": str(source), "trust": True})
    dispatch(home, "plugins.enable", {"id": "lifecycle", "trust": True})
    with pytest.raises(HarnessError, match="stopped"):
        dispatch(home, "plugins.add", {"path": str(source), "trust": True, "replace": True})
    offline_dispatch(home, "plugins.add", {"path": str(source), "trust": True, "replace": True})
    assert load_config(home)["plugins"]["lifecycle"]["enabled"] is False


@pytest.mark.asyncio
async def test_narrow_handler_and_caller_default():
    registry = Registry()
    manifest = {"provides_tools": ["test_plain"], "config_schema": {"prefix": {"type": "string", "default": "schema"}}}
    ctx = PluginContext("test", manifest, {}, registry)
    assert ctx.get_config("prefix", "caller") == "caller"
    handle = ctx.register_tool("test_plain", "test", {"name": "test_plain", "description": "Read",
        "parameters": {"type": "object", "additionalProperties": False}}, lambda args: "plain text")
    assert handle is not None
    value = await registry.execute("test_plain", {}, session_id="s", task_id="t", user_task="x")
    assert json.loads(value) == {"text": "plain text"}
    with pytest.raises(HarnessError, match="self-write"):
        ctx.set_config("prefix", "bad")
