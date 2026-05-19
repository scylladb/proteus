from __future__ import annotations

import os
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

    # 0. Env var override — highest priority after explicit --config flag
    env_path = os.environ.get("PROTEUS_CONFIG")
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p
        raise ConfigError(f"Config file not found (PROTEUS_CONFIG): {p}")

    names = ["variables.yml", "config.yml", "config.example.yml"]

    # 1. ~/.config/proteus/ — user-level pinned location, works from any directory
    user_config_dir = Path.home() / ".config" / "proteus"
    for name in names:
        p = user_config_dir / name
        if p.exists():
            return p.resolve()

    # 2. Walk up from CWD looking for a config that contains a "clusters" key
    cwd = Path.cwd()
    for directory in [cwd, *cwd.parents]:
        for name in names:
            p = directory / name
            if p.exists():
                try:
                    raw = yaml.safe_load(p.read_text()) or {}
                    if isinstance(raw, dict) and "clusters" in raw:
                        return p.resolve()
                except Exception:
                    pass
        # Stop at filesystem root or home directory
        if directory == directory.parent or directory == Path.home():
            break

    # 3. Fall back to plain CWD match (no clusters key required)
    for name in names:
        p = cwd / name
        if p.exists():
            return p.resolve()

    if allow_missing:
        return None

    raise ConfigError(
        "No config file found. Options:\n"
        "  • Set PROTEUS_CONFIG=/path/to/config.yml\n"
        "  • Place config at ~/.config/proteus/config.yml\n"
        "  • Run from the directory containing config.yml / variables.yml"
    )


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


def write_back_cluster_fields(config_path: Path, cluster_id: str, fields: dict[str, Any]) -> None:
    """Write multiple cluster fields in a single YAML round-trip."""
    data = load_config(config_path)
    clusters = data.setdefault("clusters", {})
    cluster = clusters.setdefault(cluster_id, {})
    cluster.update(fields)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False))
