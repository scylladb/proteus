from __future__ import annotations

from pathlib import Path
from typing import Any
import json


class MappingError(Exception):
    pass


def load_cloud_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MappingError(f"cloud-data file not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise MappingError(f"Invalid JSON in cloud-data file {path}: {exc}") from exc


def _is_saf_xcloud_shape(data: dict[str, Any]) -> bool:
    return "providers" in data and "regions" in data and "instances" in data


def resolve_provider_id(data: dict[str, Any], cloud: str, region: str | None = None) -> int:
    cloud = cloud.lower()
    if _is_saf_xcloud_shape(data):
        try:
            return int(data["providers"][cloud]["id"])
        except Exception as exc:
            raise MappingError(f"Provider '{cloud}' not found in x-cloud mapping data") from exc

    instances = data.get("instances", {})
    cloud_block = instances.get(cloud)
    if not isinstance(cloud_block, dict):
        raise MappingError(f"Provider '{cloud}' not found in mapping data")

    regions = [region] if region else list(cloud_block.keys())
    for r in regions:
        if r is None:
            continue
        reg = cloud_block.get(r)
        if not isinstance(reg, dict):
            continue
        for meta in reg.values():
            if isinstance(meta, dict) and "provider_id" in meta:
                return int(meta["provider_id"])

    raise MappingError(f"Unable to resolve provider_id for '{cloud}'")


def resolve_region_id(data: dict[str, Any], cloud: str, region: str) -> int:
    cloud = cloud.lower()
    if _is_saf_xcloud_shape(data):
        try:
            return int(data["regions"][cloud][region]["id"])
        except Exception as exc:
            raise MappingError(f"Region '{cloud}/{region}' not found in x-cloud mapping data") from exc

    instances = data.get("instances", {})
    region_block = (instances.get(cloud) or {}).get(region)
    if not isinstance(region_block, dict):
        raise MappingError(f"Region '{cloud}/{region}' not found in mapping data")
    for meta in region_block.values():
        if isinstance(meta, dict) and "region_id" in meta:
            return int(meta["region_id"])

    raise MappingError(f"Unable to resolve region_id for '{cloud}/{region}'")


def resolve_instance_ids(data: dict[str, Any], cloud: str, region: str, instance_types: list[str]) -> list[int]:
    cloud = cloud.lower()
    region_instances = (data.get("instances", {}) or {}).get(cloud, {}).get(region, {})
    if not isinstance(region_instances, dict):
        raise MappingError(f"No instance mapping for '{cloud}/{region}'")

    out: list[int] = []
    for name in instance_types:
        meta = region_instances.get(name)
        if not isinstance(meta, dict) or "id" not in meta:
            raise MappingError(f"Instance type '{name}' not found in '{cloud}/{region}' mapping")
        out.append(int(meta["id"]))
    return out


def resolve_family_instance_ids(data: dict[str, Any], cloud: str, region: str, families: list[str]) -> list[int]:
    cloud = cloud.lower()
    wanted = {f.strip() for f in families if f and f.strip()}
    if not wanted:
        return []

    region_instances = (data.get("instances", {}) or {}).get(cloud, {}).get(region, {})
    if not isinstance(region_instances, dict):
        raise MappingError(f"No instance mapping for '{cloud}/{region}'")

    ids: list[int] = []
    for _, meta in region_instances.items():
        if not isinstance(meta, dict):
            continue
        fam = str(meta.get("family", "")).strip()
        iid = meta.get("id")
        if fam in wanted and iid is not None:
            ids.append(int(iid))
    return sorted(set(ids))
