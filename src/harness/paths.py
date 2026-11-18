"""Private local state paths. Initialization never overwrites existing files."""
from __future__ import annotations
import json
import os
import secrets
from pathlib import Path

DEFAULT_CONFIG = {
    "revision": 1,
    "model": "qwen/qwen3-235b-a22b-2507",
    "alias": "harness-default",
    "base_url": "https://openrouter.ai/api/v1",
    "server": {"host": "127.0.0.1", "port": 8765},
    "output_tokens": 1024,
    "rounds": 8,
    "tools": 12,
    "run_timeout": 120,
    "plugins": {},
}


def home_path() -> Path:
    return Path(os.environ.get("HARNESS_HOME", "~/.harness")).expanduser().resolve()


def path_map(home: Path | None = None) -> dict:
    root = Path(home).expanduser().resolve() if home is not None else home_path()
    return {
        "home": root,
        "config": root / "config.yaml",
        "token": root / "secrets" / "local-token",
        "local_token": root / "secrets" / "local-token",
        "key": root / "secrets" / "openrouter.key",
        "database": root / "harness.sqlite3",
        "db": root / "harness.sqlite3",
        "plugins": root / "plugins",
        "plugin_data": root / "plugin-data",
        "runtime": root / "runtime",
        "owner_lock": root / "runtime" / "owner.lock",
        "owner": root / "runtime" / "owner.json",
        "owner_descriptor": root / "runtime" / "owner.json",
    }


def private_write(path: Path, content: str) -> None:
    """Atomic private replacement; used for config and descriptors, not secrets."""
    tmp = path.with_name("." + path.name + "." + secrets.token_hex(12) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def init_home(home: Path | None = None) -> dict:
    paths = path_map(home)
    for path in (paths["home"], paths["plugins"], paths["plugin_data"], paths["runtime"], paths["home"] / "secrets"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    for path, content in ((paths["config"], json.dumps(DEFAULT_CONFIG, indent=2) + "\n"), (paths["token"], secrets.token_urlsafe(32) + "\n")):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
    for key in ("config", "token", "key"):
        if paths[key].is_file():
            paths[key].chmod(0o600)
    return paths


def load_config(home: Path | None = None) -> dict:
    import copy
    import yaml
    paths = path_map(home)
    if not paths["config"].is_file():
        paths = init_home(home)
    raw = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged.update(raw)
    return merged


def owner_lock_path(home: Path | None = None) -> Path:
    return path_map(home)["owner_lock"]


def local_token_path(home: Path | None = None) -> Path:
    return path_map(home)["token"]


def owner_descriptor_path(home: Path | None = None) -> Path:
    return path_map(home)["owner_descriptor"]
