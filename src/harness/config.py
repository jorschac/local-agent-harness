from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from .errors import HarnessError
from .paths import DEFAULT_CONFIG, init_home, load_config, private_write


def validate_model(model: str) -> None:
    if not isinstance(model, str) or not model.strip():
        raise HarnessError("Model must be a nonempty string", param="model")
    # Reject all numeric variants at or beyond the prohibited families.
    for pattern, floor in ((r"gpt[-_ ]?(\d+)", 6), (r"(?:claude[-_ /]*)?fable[-_ ]?(\d+)", 5)):
        match = re.search(pattern, model.lower())
        if match and int(match.group(1)) >= floor:
            raise HarnessError("This model family/version is prohibited", "prohibited_model", param="model")


def validate_config(config: dict) -> None:
    allowed = set(DEFAULT_CONFIG)
    if set(config) - allowed:
        raise HarnessError("Unknown configuration field", param="path")
    validate_model(config["model"])
    if not isinstance(config["alias"], str) or not config["alias"].strip():
        raise HarnessError("Invalid model alias", param="alias")
    if not isinstance(config["base_url"], str) or not config["base_url"].startswith("https://"):
        raise HarnessError("Provider base_url must use HTTPS", param="base_url")
    from urllib.parse import urlparse
    parsed = urlparse(config["base_url"])
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HarnessError("Provider base_url must not contain credentials, query or fragment", param="base_url")
    server = config["server"]
    if not isinstance(server, dict) or set(server) != {"host", "port"} or server["host"] not in ("127.0.0.1", "::1", "localhost"):
        raise HarnessError("Server must bind a loopback address", param="server")
    if type(server["port"]) is not int or not 1 <= server["port"] <= 65535:
        raise HarnessError("Invalid server port", param="server.port")
    for key in ("output_tokens", "rounds", "tools"):
        if type(config[key]) is not int or config[key] < 1:
            raise HarnessError("Limit must be a positive integer", param=key)
    if type(config["run_timeout"]) not in (int, float) or not 0 < config["run_timeout"] <= 86400:
        raise HarnessError("Invalid run timeout", param="run_timeout")
    if not isinstance(config["plugins"], dict):
        raise HarnessError("Plugins configuration must be an object", param="plugins")


def change_config(home: Path, path: str, value=None, *, unset: bool = False, expected_revision: int | None = None, validate_only: bool = False) -> dict:
    config = load_config(home)
    if expected_revision is not None and expected_revision != config["revision"]:
        raise HarnessError("Configuration revision changed", "revision_conflict", 409)
    parts = path.split(".") if isinstance(path, str) else []
    if not parts or any(not p for p in parts) or parts[0] not in DEFAULT_CONFIG or parts[0] in ("revision", "plugins"):
        raise HarnessError("Unknown or managed configuration path", param="path")
    target = config
    default = copy.deepcopy(DEFAULT_CONFIG)
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict) or part not in default:
            raise HarnessError("Invalid configuration path", param="path")
        target, default = target[part], default[part]
    if parts[-1] not in target:
        raise HarnessError("Unknown configuration path", param="path")
    target[parts[-1]] = copy.deepcopy(default[parts[-1]]) if unset else value
    validate_config(config)
    if validate_only:
        return config
    config["revision"] += 1
    private_write(init_home(home)["config"], json.dumps(config, indent=2) + "\n")
    return config
