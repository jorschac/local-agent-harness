from __future__ import annotations
import contextvars
import copy
import json
import shutil
import uuid
from pathlib import Path
from .config import change_config, validate_config
from .errors import HarnessError
from .paths import init_home, load_config, private_write
from .plugins import PluginManager, read_manifest, validate_settings, package_files, package_fingerprint

OFFLINE = contextvars.ContextVar("harness_offline_management", default=False)


def owner_active(home: Path) -> bool:
    import fcntl
    import os
    path = init_home(home)["owner_lock"]
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def dispatch(home: Path, action: str, payload: dict) -> dict | list:
    """Caller serializes mutations. Offline mode never imports plugin Python."""
    home = Path(home)
    config = load_config(home)
    manager = PluginManager(home)
    if action == "config.get":
        return config
    if action in ("config.set", "config.unset"):
        root_key = str(payload.get("path", "")).split(".")[0]
        if root_key in {"server", "model", "base_url", "alias"} and not OFFLINE.get() and owner_active(home):
            change_config(home, payload.get("path"), payload.get("value"), unset=action.endswith("unset"), expected_revision=payload.get("expected_revision"), validate_only=True)
            raise HarnessError("Model route/network changes require stopped service", "restart_required", 409)
        return change_config(home, payload.get("path"), payload.get("value"), unset=action.endswith("unset"), expected_revision=payload.get("expected_revision"))
    if action == "plugins.list":
        return manager.list()
    if action == "plugins.show":
        return manager.show(payload.get("id"))
    if action == "tools.list":
        if OFFLINE.get():
            raise HarnessError("Start the service to inspect actual registered tools", "service_required", 503)
        return manager.build_registry(payload.get("plugins"), config).list()
    if action not in ("plugins.add", "plugins.enable", "plugins.disable", "plugins.settings.get", "plugins.settings.set", "plugins.settings.unset"):
        raise HarnessError("Unknown management action", "unknown_action")
    config = copy.deepcopy(config)
    plugins = config["plugins"]
    pid = payload.get("id")
    destination = None
    backup = None
    if action == "plugins.add":
        if payload.get("trust") is not True:
            raise HarnessError("Plugin Python code has full local trust; trust:true is required", "plugin_untrusted", 403)
        if not isinstance(payload.get("path"), str):
            raise HarnessError("Plugin path is required", param="path")
        original = Path(payload["path"]).expanduser()
        manifest = read_manifest(original)
        source = original.resolve()
        pid = manifest["name"]
        if pid in plugins and payload.get("replace") is not True:
            raise HarnessError("Plugin already installed; replace:true is required", "plugin_exists", 409)
        if pid in plugins and not OFFLINE.get():
            raise HarnessError("Code replacement requires stopped service", "restart_required", 409)
        previous = plugins.get(pid, {})
        settings = previous.get("settings", {})
        validate_settings(manifest, settings)
        # A bad register()/schema is discovered on activation, never by add.
        paths = init_home(home)
        destination = paths["plugins"] / pid
        staging = paths["plugins"] / (".install-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        try:
            for item in package_files(original):
                target = staging / item.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(item.read_bytes())
                target.chmod(0o600)
            read_manifest(staging)
            fingerprint = package_fingerprint(staging)
            if destination.exists():
                backup = paths["plugins"] / (".backup-" + uuid.uuid4().hex)
                destination.rename(backup)
            staging.rename(destination)
            plugins[pid] = {"path": str(destination), "source_path": str(source), "version": manifest["version"], "sha256": fingerprint, "enabled": False, "trusted": True, "settings": settings}
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            if backup is not None and backup.exists() and not destination.exists():
                backup.rename(destination)
            raise
    else:
        if pid not in plugins:
            raise HarnessError("Plugin not found", "plugin_not_found", 404)
        entry = plugins[pid]
        manifest = read_manifest(Path(entry["path"]))
        if action == "plugins.settings.get":
            return {"id": pid, "settings": dict(entry.get("settings", {})), "schema": manifest.get("config_schema", {})}
        if action == "plugins.enable":
            if payload.get("trust") is not True:
                raise HarnessError("Enable requires explicit trust:true", "plugin_untrusted", 403)
            entry["trusted"], entry["enabled"] = True, True
            if not OFFLINE.get():
                manager.build_registry(None, config)
        elif action == "plugins.disable":
            entry["enabled"] = False
        else:
            key = payload.get("key")
            if key not in manifest.get("config_schema", {}):
                raise HarnessError("Unknown plugin setting", "invalid_plugin_setting", param="key")
            entry.setdefault("settings", {})
            if action.endswith("unset"):
                entry["settings"].pop(key, None)
            else:
                entry["settings"][key] = payload.get("value")
            validate_settings(manifest, entry["settings"])
    try:
        validate_config(config)
        config["revision"] += 1
        private_write(init_home(home)["config"], json.dumps(config, indent=2) + "\n")
    except BaseException:
        if destination is not None:
            shutil.rmtree(destination, ignore_errors=True)
            if backup is not None and backup.exists():
                backup.rename(destination)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return {"id": pid, **plugins[pid], "config_revision": config["revision"], "activation": "next service start validates Python" if OFFLINE.get() and action == "plugins.enable" else "next run"}
