from __future__ import annotations
import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator, ValidationError
from .errors import HarnessError
from .paths import load_config
from .registry import PluginContext, Registry

_MODULES = {}
_LOAD_STATUS = {}
_REGISTRIES = {}
SKIP_PARTS = {"__pycache__", ".git", ".venv", ".DS_Store"}
TYPE_ALIASES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}


def package_files(path: Path):
    """Only regular contained files; generated caches are not package identity."""
    path = Path(path).expanduser()
    if path.is_symlink():
        raise HarnessError("Plugin symlinks are not supported", "invalid_plugin")
    root = path.resolve()
    files = []
    size = 0
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts) or item.suffix == ".pyc":
            continue
        if item.is_symlink():
            raise HarnessError("Plugin must not contain symlinks", "invalid_plugin")
        if item.is_dir():
            continue
        if not item.is_file():
            raise HarnessError("Only regular plugin files are supported", "invalid_plugin")
        size += item.stat().st_size
        files.append(item)
        if size > 20_000_000 or len(files) > 1024:
            raise HarnessError("Plugin package exceeds local size limit", "invalid_plugin")
    return files


def package_fingerprint(path: Path) -> str:
    root = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    for item in package_files(path):
        digest.update(str(item.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def setting_schema(spec: dict) -> dict:
    schema = {key: value for key, value in spec.items() if key not in {"required", "choices", "secret"}}
    if "type" in schema:
        schema["type"] = TYPE_ALIASES.get(schema["type"], schema["type"])
    if "choices" in spec:
        schema.setdefault("enum", spec["choices"])
    return schema


def read_manifest(path: Path) -> dict:
    original = Path(path).expanduser()
    package_files(original)
    path = original.resolve()
    if not path.is_dir() or not (path / "__init__.py").is_file():
        raise HarnessError("Plugin directory must contain __init__.py", "invalid_plugin", param="path")
    manifest_path = path / "plugin.yaml"
    if not manifest_path.is_file():
        manifest_path = path / "plugin.yml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        raise HarnessError("Cannot read plugin.yaml", "invalid_plugin", param="path") from None
    if not isinstance(manifest, dict) or manifest.get("manifest_version") not in (1, 2) or manifest.get("kind") != "standalone":
        raise HarnessError("Only standalone manifest_version 1/2 plugins are supported", "invalid_plugin")
    fields = {"name", "version", "description", "kind", "manifest_version", "provides_tools", "provides_hooks", "requires_env", "config_schema", "author", "license", "homepage", "tags"}
    if set(manifest) - fields:
        raise HarnessError("Unsupported manifest field or extension surface", "invalid_plugin")
    name = manifest.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name):
        raise HarnessError("Invalid plugin name", "invalid_plugin")
    if not isinstance(manifest.get("version"), str) or not manifest["version"] or not isinstance(manifest.get("description"), str) or not manifest["description"]:
        raise HarnessError("Manifest requires nonempty string version and description", "invalid_plugin")
    tools = manifest.get("provides_tools")
    if not isinstance(tools, list) or not tools or any(not isinstance(n, str) for n in tools) or len(set(tools)) != len(tools):
        raise HarnessError("provides_tools must be a nonempty unique name array", "invalid_plugin")
    if manifest.get("provides_hooks", []) != []:
        raise HarnessError("Host hooks are not supported", "invalid_plugin")
    env = manifest.get("requires_env", [])
    if not isinstance(env, list):
        raise HarnessError("Invalid manifest requires_env", "invalid_plugin")
    env_names = []
    for value in env:
        name = value if isinstance(value, str) else value.get("name") if isinstance(value, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise HarnessError("Invalid environment name", "invalid_plugin")
        env_names.append(name)
    manifest["requires_env"] = env_names
    schema = manifest.get("config_schema", {})
    if not isinstance(schema, dict):
        raise HarnessError("Invalid config_schema", "invalid_plugin")
    for key, spec in schema.items():
        if not isinstance(key, str) or not key or not isinstance(spec, dict) or spec.get("secret"):
            raise HarnessError("Invalid or secret config_schema entry", "invalid_plugin")
        try:
            Draft202012Validator.check_schema(setting_schema(spec))
        except Exception:
            raise HarnessError("Invalid setting schema", "invalid_plugin") from None
    return manifest


def validate_settings(manifest: dict, settings: dict):
    schema = manifest.get("config_schema", {})
    if not isinstance(settings, dict) or set(settings) - set(schema):
        raise HarnessError("Unknown plugin setting", "invalid_plugin_setting")
    for key, spec in schema.items():
        if spec.get("required") is True and key not in settings and "default" not in spec:
            raise HarnessError("Required plugin setting is missing", "invalid_plugin_setting", param=key)
    for key, value in settings.items():
        try:
            Draft202012Validator(setting_schema(schema[key])).validate(value)
        except ValidationError:
            raise HarnessError("Plugin setting does not match its schema", "invalid_plugin_setting", param=key) from None


def import_plugin(path: Path, fingerprint: str):
    path = path.resolve()
    key = (str(path), fingerprint)
    if key in _MODULES:
        return _MODULES[key]
    namespace = "harness_plugin_" + hashlib.sha256(repr(key).encode()).hexdigest()[:20]
    spec = importlib.util.spec_from_file_location(namespace, path / "__init__.py", submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    try:
        spec.loader.exec_module(module)
        if not callable(getattr(module, "register", None)):
            raise ValueError("Missing register")
    except (Exception, SystemExit):
        for module_name in list(sys.modules):
            if module_name == namespace or module_name.startswith(namespace + "."):
                sys.modules.pop(module_name, None)
        raise HarnessError("Plugin import/register entry point failed", "invalid_plugin") from None
    _MODULES[key] = module
    return module


class PluginManager:
    def __init__(self, home: Path):
        self.home = Path(home)

    def build_registry(self, selected: list[str] | None = None, config: dict | None = None) -> Registry:
        config = config if config is not None else load_config(self.home)
        installed = config.get("plugins", {})
        ids = [pid for pid, entry in installed.items() if entry.get("enabled")] if selected is None else list(selected)
        if len(set(ids)) != len(ids):
            raise HarnessError("Duplicate plugin selection", "invalid_plugin_selection")
        key = (str(self.home), config["revision"], tuple(ids))
        if key in _REGISTRIES:
            for pid in ids:
                entry = installed[pid]
                if package_fingerprint(Path(entry["path"])) != entry.get("sha256"):
                    raise HarnessError("Plugin contents changed; stop service and reinstall", "plugin_changed", 409)
                manifest = read_manifest(Path(entry["path"]))
                if any(not os.environ.get(name) for name in manifest.get("requires_env", [])):
                    raise HarnessError("Required plugin environment missing", "missing_environment")
            return _REGISTRIES[key]
        registry = Registry()
        for pid in ids:
            entry = installed.get(pid)
            if entry is None:
                raise HarnessError("Plugin is not installed: " + pid, "plugin_not_found", 404)
            if not entry.get("trusted"):
                raise HarnessError("Explicit full-trust consent is required", "plugin_untrusted", 403)
            path = Path(entry["path"])
            manifest = read_manifest(path)
            fingerprint = package_fingerprint(path)
            if not entry.get("sha256") or fingerprint != entry["sha256"]:
                raise HarnessError("Plugin contents changed; stop service and reinstall", "plugin_changed", 409)
            if manifest["name"] != pid:
                raise HarnessError("Installed plugin identity changed", "invalid_plugin")
            settings = dict(entry.get("settings", {}))
            validate_settings(manifest, settings)
            missing = [key for key in manifest.get("requires_env", []) if not os.environ.get(key)]
            if missing:
                raise HarnessError("Missing required plugin environment: " + ", ".join(missing), "missing_environment")
            before = set(registry.tools)
            try:
                import_plugin(path, fingerprint).register(PluginContext(pid, manifest, settings, registry, home=self.home))
                names = set(registry.tools) - before
                if names != set(manifest["provides_tools"]):
                    raise HarnessError("Registered tools differ from manifest", "invalid_plugin")
            except (Exception, SystemExit) as exc:
                _LOAD_STATUS[(str(self.home), pid)] = {"loaded": False, "compatible": False, "error": getattr(exc, "code", "invalid_plugin"), "actual_tools": []}
                if isinstance(exc, HarnessError):
                    raise
                raise HarnessError("Plugin registration failed", "invalid_plugin") from None
            _LOAD_STATUS[(str(self.home), pid)] = {"loaded": True, "compatible": True, "error": None, "actual_tools": sorted(names)}
        _REGISTRIES[key] = registry
        for older in list(_REGISTRIES):
            if older[0] == str(self.home) and older[1] < config["revision"] - 8:
                _REGISTRIES.pop(older, None)
        return registry

    def list(self) -> list[dict]:
        config = load_config(self.home)
        return [self.show(pid, config) for pid in config["plugins"]]

    def show(self, pid: str, config: dict | None = None) -> dict:
        config = config or load_config(self.home)
        entry = config["plugins"].get(pid)
        if entry is None:
            raise HarnessError("Plugin not found", "plugin_not_found", 404)
        result = {"id": pid, **entry, "registered": True, "default_enabled": entry["enabled"], **_LOAD_STATUS.get((str(self.home), pid), {"loaded": False, "compatible": None, "actual_tools": [], "error": None})}
        try:
            result["manifest"] = read_manifest(Path(entry["path"]))
            if package_fingerprint(Path(entry["path"])) != entry.get("sha256"):
                result.update(compatible=False, error="plugin_changed")
        except HarnessError as exc:
            result.update(compatible=False, error=exc.code)
        return result
