from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


def resolve_config_path(explicit_path: str | None, allow_missing: bool = False) -> Path | None:
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        return p

    candidates = [
        Path.cwd() / "variables.yml",
        Path.cwd() / "config.yml",
        Path.cwd() / "config.example.yml",
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()

    if allow_missing:
        return None

    raise ConfigError("No config file found. Expected one of: variables.yml, config.yml, config.example.yml")


def load_config(config_path: Path | None) -> dict[str, Any]:
    if config_path is None:
        return {}

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping: {config_path}")

    clusters = raw.get("clusters")
    if clusters is not None and not isinstance(clusters, dict):
        raise ConfigError("'clusters' must be a mapping when present in config")

    return raw


def get_cluster(conf: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    clusters = conf.get("clusters") or {}
    cluster = clusters.get(cluster_id)
    if not cluster:
        raise ConfigError(f"Cluster '{cluster_id}' not found in config")
    if not isinstance(cluster, dict):
        raise ConfigError(f"Cluster '{cluster_id}' must be a mapping")
    return cluster


def write_back_cluster_field(config_path: Path, cluster_id: str, key: str, value: Any) -> None:
    data = load_config(config_path)
    clusters = data.setdefault("clusters", {})
    cluster = clusters.setdefault(cluster_id, {})
    cluster[key] = value
    config_path.write_text(yaml.safe_dump(data, sort_keys=False))
