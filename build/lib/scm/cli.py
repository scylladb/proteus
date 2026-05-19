from __future__ import annotations

import argparse
import json
import os
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import APIError, ScyllaCloudAPI
from .config import ConfigError, get_cluster, load_config, resolve_config_path, write_back_cluster_field
from .errors import decode_api_error, load_error_catalog
from .mapping import (
    MappingError,
    load_cloud_data,
    resolve_family_instance_ids,
    resolve_instance_ids,
    resolve_provider_id,
    resolve_region_id,
)


def _die(msg: str, code: int = 1) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _parse_csv_int(value: str | None) -> list[int]:
    return [int(x) for x in _parse_csv(value)]


def _pick_cluster_ref(args: argparse.Namespace) -> str:
    cluster_ref = getattr(args, "cluster_id", None) or getattr(args, "clusterid", None)
    if not cluster_ref:
        _die("Missing cluster reference. Use positional <cluster-id> or --clusterid")
    return cluster_ref


def _merge_cluster_overrides(base: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    out = deepcopy(base)
    gv = lambda name, default=None: getattr(args, name, default)

    simple_overrides = {
        "cluster_name": gv("cluster_name"),
        "cluster_type": gv("cluster_type"),
        "cloud": gv("cloud"),
        "region": gv("region"),
        "scylla_version": gv("scylla_version"),
        "api_interface": gv("api_interface"),
        "replication_factor": gv("replication_factor"),
        "broadcast_type": gv("broadcast_type"),
        "cidr_block": gv("cidr_block"),
        "existing_cluster_id": gv("existing_cluster_id"),
    }
    for k, v in simple_overrides.items():
        if v is not None:
            out[k] = v

    if gv("cloud_provider_id") is not None or gv("region_id") is not None:
        resolved_ids = dict(out.get("resolved_ids") or {})
        if gv("cloud_provider_id") is not None:
            resolved_ids["cloud_provider_id"] = int(gv("cloud_provider_id"))
        if gv("region_id") is not None:
            resolved_ids["region_id"] = int(gv("region_id"))
        out["resolved_ids"] = resolved_ids

    instance_families = _parse_csv(gv("instance_families"))
    instance_types = _parse_csv(gv("instance_types"))
    instance_type_ids = _parse_csv_int(gv("instance_type_ids"))
    if gv("wanted_size"):
        if str(out.get("cluster_type") or "") == "x-cloud":
            instance_types = [gv("wanted_size")]
        else:
            setattr(args, "node_type", gv("wanted_size"))

    scaling_touched = any(
        [
            instance_families,
            instance_types,
            instance_type_ids,
            gv("storage_min_gb") is not None,
            gv("storage_target_utilization") is not None,
            gv("vcpu_min") is not None,
        ]
    )
    if scaling_touched:
        scaling = dict(out.get("scaling") or {})
        if instance_families:
            scaling["instance_families"] = instance_families
        if instance_types:
            scaling["instance_types"] = instance_types
        if instance_type_ids:
            scaling["instance_type_ids"] = instance_type_ids

        if gv("storage_min_gb") is not None or gv("storage_target_utilization") is not None:
            storage = dict(scaling.get("storage") or {})
            if gv("storage_min_gb") is not None:
                storage["min_gb"] = int(gv("storage_min_gb"))
            if gv("storage_target_utilization") is not None:
                storage["target_utilization"] = float(gv("storage_target_utilization"))
            scaling["storage"] = storage

        if gv("vcpu_min") is not None:
            vcpu = dict(scaling.get("vcpu") or {})
            vcpu["min"] = int(gv("vcpu_min"))
            scaling["vcpu"] = vcpu

        out["scaling"] = scaling

    if gv("wanted_count") is not None and gv("node_count") is None:
        setattr(args, "node_count", gv("wanted_count"))

    node_group_touched = any(
        [
            gv("node_count") is not None,
            gv("node_type") is not None,
            gv("node_type_id") is not None,
        ]
    )
    if node_group_touched:
        node_groups = list(out.get("node_groups") or [])
        primary = dict(node_groups[0] if node_groups else {"name": "primary"})
        if gv("node_count") is not None:
            primary["count"] = int(gv("node_count"))
        if gv("node_type") is not None:
            primary["node_type"] = gv("node_type")
        if gv("node_type_id") is not None:
            primary["node_type_id"] = int(gv("node_type_id"))

        if node_groups:
            node_groups[0] = primary
        else:
            node_groups = [primary]
        out["node_groups"] = node_groups

    return out


def _cluster_from_sources(conf: dict[str, Any], args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    cluster_ref = _pick_cluster_ref(args)
    base = {}
    clusters = conf.get("clusters") or {}
    if cluster_ref in clusters and isinstance(clusters[cluster_ref], dict):
        base = clusters[cluster_ref]
    return cluster_ref, _merge_cluster_overrides(base, args)


def _api_settings(conf: dict[str, Any], args: argparse.Namespace | None = None) -> tuple[str, int, bool]:
    api = conf.get("api") or {}
    token = str((getattr(args, "api_token", None) if args else None) or api.get("token") or "").strip()
    if token.startswith("${") and token.endswith("}"):
        env_name = token[2:-1]
        token = str(os.environ.get(env_name, "")).strip()
    if not token:
        token = str(os.environ.get("SCYLLA_CLOUD_API_TOKEN", "")).strip()
    if not token:
        _die("Missing API token. Set api.token or env var.")

    timeout = int((getattr(args, "api_timeout", None) if args else None) or api.get("timeout", 300))
    ssl_verify = bool(api.get("ssl_verify", True))
    if args and getattr(args, "no_ssl_verify", False):
        ssl_verify = False
    return token, timeout, ssl_verify


def _paths(conf: dict[str, Any], config_path: Path | None, args: argparse.Namespace | None = None) -> tuple[Path, Path]:
    refs = conf.get("reference_data") or {}
    base_dir = config_path.parent if config_path else Path.cwd()
    cloud_data_path = Path(
        (getattr(args, "cloud_data", None) if args else None)
        or refs.get("cloud_data_path")
        or "./cloud-data.json"
    )
    err_path = Path(
        (getattr(args, "api_error_codes", None) if args else None)
        or refs.get("api_error_codes_path")
        or "./api_error_codes.tsv"
    )

    if not cloud_data_path.is_absolute():
        cloud_data_path = (base_dir / cloud_data_path).resolve()
    if not err_path.is_absolute():
        err_path = (base_dir / err_path).resolve()

    return cloud_data_path, err_path


def _resolve_ids(cluster: dict[str, Any], cloud_data: dict[str, Any]) -> tuple[int, int]:
    cloud = str(cluster.get("cloud") or "").strip().lower()
    region = str(cluster.get("region") or "").strip()
    if not cloud or not region:
        raise ConfigError("Cluster requires both cloud and region")

    resolved = cluster.get("resolved_ids") or {}
    provider_id = resolved.get("cloud_provider_id")
    region_id = resolved.get("region_id")

    if provider_id is None:
        provider_id = resolve_provider_id(cloud_data, cloud, region)
    if region_id is None:
        region_id = resolve_region_id(cloud_data, cloud, region)

    return int(provider_id), int(region_id)


def _account_id(api: ScyllaCloudAPI) -> int:
    resp = api.get_account_default()
    data = resp.get("data") or {}
    account_id = data.get("accountId")
    if account_id is None:
        _die(f"Unable to resolve account ID from /account/default response: {resp}")
    return int(account_id)


def _cloud_credential_id(api: ScyllaCloudAPI, account_id: int, provider_id: int) -> int:
    resp = api.get_cloud_accounts(account_id)
    items = resp.get("data") or []
    if not isinstance(items, list):
        _die(f"Unexpected cloud-account response shape: {resp}")

    for item in items:
        if int(item.get("cloudProviderId", -1)) == provider_id:
            return int(item["id"])
    _die(f"No cloud account credential found for cloudProviderId={provider_id}")
    return -1


def _cluster_numeric_id(cluster: dict[str, Any]) -> int:
    cid = cluster.get("existing_cluster_id")
    if cid in (None, "", 0):
        raise ConfigError("Cluster has no existing_cluster_id. Run setup first or set it in config.")
    return int(cid)


def _decode_and_maybe_fail(resp: dict[str, Any], catalog: dict[str, str]) -> None:
    err = resp.get("error")
    if err and str(err).strip() not in ("", "null"):
        decoded = decode_api_error(err, catalog)
        _die(f"API error: {decoded}\nResponse: {json.dumps(resp, indent=2)}")

def _extract_request_id(resp: dict[str, Any]) -> int | None:
    data = resp.get("data") or {}
    for key in ("requestId", "id"):
        if data.get(key) is not None:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                pass
    fields = data.get("fields") or {}
    for key in ("requestId", "id"):
        if fields.get(key) is not None:
            try:
                return int(fields[key])
            except (TypeError, ValueError):
                pass
    return None


def _wait_for_request(
    api: ScyllaCloudAPI,
    account_id: int,
    request_id: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        resp = api.get_cluster_request(account_id, request_id)
        data = resp.get("data") or {}
        status = str(data.get("status") or "UNKNOWN").upper()
        pct = data.get("progressPercent")
        msg = data.get("progressDescription") or ""
        pct_part = f" {pct}%" if pct is not None else ""
        print(f"Request {request_id} status={status}{pct_part} {msg}".rstrip())

        if status in ("COMPLETED", "DONE", "SUCCEEDED"):
            return resp
        if status in ("FAILED", "ERROR", "CANCELED"):
            _die(f"Request {request_id} failed: {json.dumps(resp, indent=2)}")

        if time.monotonic() >= deadline:
            _die(f"Timed out waiting for request {request_id}")

        time.sleep(max(1, poll_interval_seconds))


def _is_active_request_status(status: Any) -> bool:
    value = str(status or "").strip().upper()
    if not value:
        return False
    if value in ("COMPLETED", "DONE", "SUCCEEDED", "FAILED", "ERROR", "CANCELED", "CANCELLED"):
        return False
    return value in {
        "PENDING",
        "QUEUED",
        "IN_PROGRESS",
        "INPROGRESS",
        "RUNNING",
        "PROCESSING",
        "CREATING",
        "UPDATING",
        "RESIZING",
        "DELETING",
        "CANCELING",
        "CANCELLING",
        "RETRYING",
    }


def _collect_request_ids(payload: Any) -> set[int]:
    ids: set[int] = set()
    keys = {
        "requestid",
        "request_id",
        "currentrequestid",
        "current_request_id",
        "activerequestid",
        "active_request_id",
        "ongoingrequestid",
        "ongoing_request_id",
        "lastrequestid",
        "last_request_id",
    }

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                k_norm = str(k).strip().lower()
                if k_norm in keys and v is not None:
                    try:
                        ids.add(int(v))
                    except (TypeError, ValueError):
                        pass
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return ids


def _find_embedded_active_status(payload: Any) -> str | None:
    status_keys = {
        "requeststatus",
        "request_status",
        "currentrequeststatus",
        "current_request_status",
        "operationstatus",
        "operation_status",
        "currentoperationstatus",
        "current_operation_status",
    }

    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).strip().lower()
                if key in status_keys and _is_active_request_status(v):
                    return str(v)
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def _prevalidate_no_active_cluster_request(api: ScyllaCloudAPI, account_id: int, cluster_id: int) -> None:
    cluster_resp = api.get_cluster(account_id, cluster_id, enriched=True)
    cluster_data = cluster_resp.get("data") or {}

    request_ids = sorted(_collect_request_ids(cluster_data))
    for request_id in request_ids:
        try:
            request_resp = api.get_cluster_request(account_id, request_id)
        except APIError:
            continue
        request_data = request_resp.get("data") or {}
        request_status = request_data.get("status")
        if _is_active_request_status(request_status):
            _die(
                f"Cluster {cluster_id} already has an active request "
                f"(requestId={request_id}, status={request_status}). "
                "Wait for it to complete before submitting another request."
            )

    embedded_status = _find_embedded_active_status(cluster_data)
    if embedded_status:
        _die(
            f"Cluster {cluster_id} appears to have an active request (status={embedded_status}). "
            "Wait for it to complete before submitting another request."
        )


def _build_create_payload(
    cluster_id: str,
    cluster: dict[str, Any],
    cloud_data: dict[str, Any],
    provider_id: int,
    region_id: int,
    account_credential_id: int,
) -> dict[str, Any]:
    cluster_type = str(cluster.get("cluster_type") or "").strip()
    if cluster_type not in ("x-cloud", "scylla-cloud"):
        raise ConfigError(f"{cluster_id}: cluster_type must be x-cloud or scylla-cloud")

    cluster_name = str(cluster.get("cluster_name") or "").strip()
    if not cluster_name:
        raise ConfigError(f"{cluster_id}: cluster_name is required")

    payload: dict[str, Any] = {
        "accountCredentialId": account_credential_id,
        "broadcastType": str(cluster.get("broadcast_type") or "PRIVATE").upper(),
        "cidrBlock": str(cluster.get("cidr_block") or ""),
        "cloudProviderId": provider_id,
        "regionId": region_id,
        "clusterName": cluster_name,
        "replicationFactor": int(cluster.get("replication_factor", 3)),
        "scyllaVersion": str(cluster.get("scylla_version") or ""),
        "userApiInterface": str(cluster.get("api_interface") or "CQL").upper(),
        "freeTier": False,
    }

    if not payload["cidrBlock"]:
        raise ConfigError(f"{cluster_id}: cidr_block is required")
    if not payload["scyllaVersion"]:
        raise ConfigError(f"{cluster_id}: scylla_version is required")

    cloud = str(cluster.get("cloud") or "").strip().lower()
    region = str(cluster.get("region") or "").strip()

    if cluster_type == "x-cloud":
        scaling = cluster.get("scaling") or {}
        families = [x for x in (scaling.get("instance_families") or []) if x]
        names = [x for x in (scaling.get("instance_types") or []) if x]
        ids = [int(x) for x in (scaling.get("instance_type_ids") or []) if x is not None]

        if not ids and names:
            ids = resolve_instance_ids(cloud_data, cloud, region, names)
        if not ids and families:
            ids = resolve_family_instance_ids(cloud_data, cloud, region, families)

        storage = scaling.get("storage") or {}
        target = storage.get("target_utilization", 80)
        target = float(target) / 100.0 if float(target) > 1 else float(target)

        payload["scaling"] = {
            "instanceFamilies": families,
            "instanceTypeIDs": ids,
            "mode": "xcloud",
            "policies": {
                "storage": {
                    "min": int(storage.get("min_gb", 0)),
                    "targetUtilization": target,
                },
                "vcpu": {
                    "min": int((scaling.get("vcpu") or {}).get("min", 0)),
                },
            },
        }
    else:
        node_groups = cluster.get("node_groups") or []
        if not node_groups:
            raise ConfigError(f"{cluster_id}: node_groups required for scylla-cloud")

        primary = node_groups[0]
        node_count = int(primary.get("count", 0))
        if node_count <= 0:
            raise ConfigError(f"{cluster_id}: primary node group count must be > 0")

        instance_id = primary.get("node_type_id")
        if instance_id is None:
            node_type = str(primary.get("node_type") or "").strip()
            if not node_type:
                raise ConfigError(f"{cluster_id}: node_groups[0].node_type is required")
            instance_id = resolve_instance_ids(cloud_data, cloud, region, [node_type])[0]

        payload["numberOfNodes"] = node_count
        payload["instanceId"] = int(instance_id)

    return payload


def _build_resize_payload(cluster: dict[str, Any], cloud_data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    cluster_type = str(cluster.get("cluster_type") or "").strip()
    cloud = str(cluster.get("cloud") or "").strip().lower()
    region = str(cluster.get("region") or "").strip()

    if cluster_type == "x-cloud":
        scaling = cluster.get("scaling") or {}
        families = [x for x in (scaling.get("instance_families") or []) if x]
        names = [x for x in (scaling.get("instance_types") or []) if x]
        ids = [int(x) for x in (scaling.get("instance_type_ids") or []) if x is not None]

        if not ids and names:
            ids = resolve_instance_ids(cloud_data, cloud, region, names)
        if not ids and families:
            ids = resolve_family_instance_ids(cloud_data, cloud, region, families)

        storage = scaling.get("storage") or {}
        target = storage.get("target_utilization", 80)
        target = float(target) / 100.0 if float(target) > 1 else float(target)

        payload = {
            "instanceFamilies": families,
            "instanceTypeIDs": ids,
            "policies": {
                "storage": {
                    "min": int(storage.get("min_gb", 0)),
                    "targetUtilization": target,
                },
                "vcpu": {
                    "min": int((scaling.get("vcpu") or {}).get("min", 0)),
                },
            },
        }
        return "x-cloud", payload

    if cluster_type == "scylla-cloud":
        node_groups = cluster.get("node_groups") or []
        if not node_groups:
            raise ConfigError("node_groups required for scylla-cloud resize")

        primary = node_groups[0]
        node_count = int(primary.get("count", 0))
        if node_count <= 0:
            raise ConfigError("node_groups[0].count must be > 0")

        instance_id = primary.get("node_type_id")
        if instance_id is None:
            node_type = str(primary.get("node_type") or "").strip()
            instance_id = resolve_instance_ids(cloud_data, cloud, region, [node_type])[0]

        payload = {
            "dcNodes": [
                {
                    "nodeCount": node_count,
                    "instanceTypeId": int(instance_id),
                }
            ]
        }
        return "scylla-cloud", payload

    raise ConfigError("cluster_type must be x-cloud or scylla-cloud")


def _print_json(title: str, payload: dict[str, Any]) -> None:
    print(title)
    print(json.dumps(payload, indent=2, sort_keys=False))


def cmd_setup(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

    if cluster.get("existing_cluster_id"):
        print(
            f"Cluster '{cluster_ref}' already linked via existing_cluster_id={cluster['existing_cluster_id']}. "
            "Skipping create."
        )
        return

    token, timeout, ssl_verify = _api_settings(conf, args)
    cloud_data_path, err_path = _paths(conf, config_path, args)
    cloud_data = load_cloud_data(cloud_data_path)
    catalog = load_error_catalog(err_path)

    provider_id, region_id = _resolve_ids(cluster, cloud_data)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    account_credential_id = _cloud_credential_id(api, account_id, provider_id)

    payload = _build_create_payload(
        cluster_ref,
        cluster,
        cloud_data,
        provider_id,
        region_id,
        account_credential_id,
    )

    _print_json("Create payload:", payload)
    if args.dry_run:
        print("Dry-run: no API call made.")
        return

    resp = api.create_cluster(account_id, payload)
    _decode_and_maybe_fail(resp, catalog)
    _print_json("Create response:", resp)

    request_id = _extract_request_id(resp)
    if args.wait and request_id is not None:
        final = _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval)
        _print_json("Final request status:", final)

    data = resp.get("data") or {}
    candidate_id = data.get("clusterId") or data.get("id")
    if candidate_id is None:
        fields = data.get("fields") or {}
        candidate_id = fields.get("clusterId") or fields.get("id")

    if candidate_id is not None and args.write_back and config_path is not None:
        write_back_cluster_field(config_path, cluster_ref, "existing_cluster_id", int(candidate_id))
        print(f"Updated config with existing_cluster_id={candidate_id}")
    elif candidate_id is not None and args.write_back and config_path is None:
        print("Skipping --write-back because no config file was loaded.")


def cmd_resize(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    _, cluster = _cluster_from_sources(conf, args)

    token, timeout, ssl_verify = _api_settings(conf, args)
    cloud_data_path, err_path = _paths(conf, config_path, args)
    cloud_data = load_cloud_data(cloud_data_path)
    catalog = load_error_catalog(err_path)

    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    cluster_id = _cluster_numeric_id(cluster)
    _prevalidate_no_active_cluster_request(api, account_id, cluster_id)

    mode, payload = _build_resize_payload(cluster, cloud_data)

    if mode == "x-cloud":
        dcs_resp = api.get_cluster_dcs(account_id, cluster_id, enriched=True)
        dcs = ((dcs_resp.get("data") or {}).get("dataCenters") or [])
        if not dcs:
            _die("No datacenters returned for cluster; cannot update scaling policy")
        dc_id = dcs[0].get("id")
        if dc_id is None:
            _die(f"Unable to resolve dc id from response: {dcs_resp}")

        _print_json(f"DC scaling payload (dcId={dc_id}):", payload)
        if args.dry_run:
            print("Dry-run: no API call made.")
            return

        resp = api.update_dc_scaling(account_id, cluster_id, dc_id, payload)
    else:
        dcs_resp = api.get_cluster_dcs(account_id, cluster_id, enriched=True)
        dcs = ((dcs_resp.get("data") or {}).get("dataCenters") or [])
        if not dcs:
            _die("No datacenters returned for cluster; cannot resize")
        dc_id = dcs[0].get("id")
        if dc_id is None:
            _die(f"Unable to resolve dc id from response: {dcs_resp}")
        payload["dcNodes"][0]["dcId"] = int(dc_id)

        _print_json("Resize payload:", payload)
        if args.dry_run:
            print("Dry-run: no API call made.")
            return

        resp = api.resize_cluster(account_id, cluster_id, payload)

    _decode_and_maybe_fail(resp, catalog)
    _print_json("Resize response:", resp)

    request_id = _extract_request_id(resp)
    if args.wait and request_id is not None:
        final = _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval)
        _print_json("Final request status:", final)


def cmd_destroy(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    _, cluster = _cluster_from_sources(conf, args)

    if not args.yes and not args.dry_run:
        _die("Destroy requires --yes confirmation")

    token, timeout, ssl_verify = _api_settings(conf, args)
    cloud_data_path, err_path = _paths(conf, config_path, args)
    _ = load_cloud_data(cloud_data_path)
    catalog = load_error_catalog(err_path)

    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    cluster_id = _cluster_numeric_id(cluster)
    _prevalidate_no_active_cluster_request(api, account_id, cluster_id)
    cluster_name = str(cluster.get("cluster_name") or "").strip()
    if not cluster_name:
        _die("cluster_name is required for destroy")

    if args.dry_run:
        print(f"Dry-run: would delete cluster id={cluster_id}, name={cluster_name}")
        return

    resp = api.delete_cluster(account_id, cluster_id, cluster_name)
    _decode_and_maybe_fail(resp, catalog)
    _print_json("Destroy response:", resp)

    request_id = _extract_request_id(resp)
    if args.wait and request_id is not None:
        final = _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval)
        _print_json("Final request status:", final)


def cmd_status(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    _, cluster = _cluster_from_sources(conf, args)

    token, timeout, ssl_verify = _api_settings(conf, args)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    cluster_id = _cluster_numeric_id(cluster)
    resp = api.get_cluster(account_id, cluster_id, enriched=True)
    _print_json("Cluster status:", resp)


def cmd_list(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)

    token, timeout, ssl_verify = _api_settings(conf, args)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    resp = api.list_clusters(account_id, enriched=True)
    _print_json("Cloud clusters:", resp)


def cmd_validate(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cloud_data_path, err_path = _paths(conf, config_path, args)

    cloud_data = load_cloud_data(cloud_data_path)
    catalog = load_error_catalog(err_path)

    cluster_ids: list[str]
    if args.cluster_ids:
        cluster_ids = args.cluster_ids
    else:
        cluster_ids = list((conf.get("clusters") or {}).keys())
        if not cluster_ids and (args.cluster_id or args.clusterid):
            cluster_ids = [_pick_cluster_ref(args)]

    if not cluster_ids:
        _die("No clusters to validate. Provide cluster IDs or a config with clusters.")

    problems: list[str] = []

    for cid in cluster_ids:
        try:
            base = {}
            if cid in (conf.get("clusters") or {}):
                base = get_cluster(conf, cid)
            cluster = _merge_cluster_overrides(base, args)
            provider_id, region_id = _resolve_ids(cluster, cloud_data)
            ctype = str(cluster.get("cluster_type") or "")
            if ctype not in ("x-cloud", "scylla-cloud"):
                raise ConfigError("cluster_type must be x-cloud or scylla-cloud")
            _ = provider_id, region_id
        except (ConfigError, MappingError) as exc:
            problems.append(f"[{cid}] {exc}")

    if problems:
        print("Validation failed:")
        for p in problems:
            print(f"- {p}")
        raise SystemExit(2)

    print(f"Validation OK for {len(cluster_ids)} cluster(s)")
    print(f"Loaded {len(catalog)} API error code mappings from {err_path}")


def cmd_cache_refresh_cloud(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    token, timeout, ssl_verify = _api_settings(conf, args)
    cloud_data_path, _ = _paths(conf, config_path, args)

    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    providers_resp = api.get_cloud_providers()
    providers = ((providers_resp.get("data") or {}).get("cloudProviders") or [])

    out: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "instances": {},
    }

    for p in providers:
        pname = str(p.get("name") or "").lower()
        if "amazon" in pname or "aws" in pname:
            cloud_name = "aws"
        elif "google" in pname or "gcp" in pname:
            cloud_name = "gcp"
        else:
            continue

        provider_id = int(p["id"])
        reg_resp = api.get_provider_regions(provider_id)
        regions = ((reg_resp.get("data") or {}).get("regions") or [])
        cloud_block: dict[str, Any] = {}

        for reg in regions:
            region_name = reg.get("externalId")
            region_id = reg.get("id")
            if not region_name or region_id is None:
                continue

            inst_resp = api.get_instances_for_region(provider_id, int(region_id))
            instances = ((inst_resp.get("data") or {}).get("instances") or [])
            region_instances: dict[str, Any] = {}
            for inst in instances:
                ext = inst.get("externalId")
                iid = inst.get("id")
                if not ext or iid is None:
                    continue
                family = ext.split(".")[0] if "." in ext else ext.split("-")[0]
                region_instances[str(ext)] = {
                    "family": family,
                    "id": int(iid),
                    "provider_id": provider_id,
                    "region_id": int(region_id),
                }
            cloud_block[str(region_name)] = region_instances

        out["instances"][cloud_name] = cloud_block

    cloud_data_path.parent.mkdir(parents=True, exist_ok=True)
    cloud_data_path.write_text(json.dumps(out, indent=2, sort_keys=True))
    print(f"Wrote cloud mapping cache to {cloud_data_path}")


def cmd_cloud_data(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cloud_data_path, _ = _paths(conf, config_path, args)
    cloud_data = load_cloud_data(cloud_data_path)

    cloud = args.cloud.lower()
    region = args.region
    region_instances = ((cloud_data.get("instances") or {}).get(cloud) or {}).get(region)
    if not isinstance(region_instances, dict) or not region_instances:
        _die(f"No mapping data found for {cloud}/{region} in {cloud_data_path}")

    if args.families_only:
        families = sorted({str(meta.get("family", "")) for meta in region_instances.values() if isinstance(meta, dict)})
        print(f"Families for {cloud}/{region}:")
        for fam in families:
            if fam:
                print(f"- {fam}")
        return

    rows = []
    for name, meta in sorted(region_instances.items()):
        if not isinstance(meta, dict):
            continue
        rows.append(
            {
                "instance": name,
                "id": meta.get("id"),
                "family": meta.get("family"),
                "provider_id": meta.get("provider_id"),
                "region_id": meta.get("region_id"),
            }
        )
    print(json.dumps(rows, indent=2))


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-token", help="Override API token (else api.token/env is used)")
    parser.add_argument("--api-timeout", type=int, help="API timeout seconds override")
    parser.add_argument("--no-ssl-verify", action="store_true", help="Disable TLS certificate validation")
    parser.add_argument("--cloud-data", help="Path to cloud-data.json override")
    parser.add_argument("--api-error-codes", help="Path to api_error_codes.tsv override")


def _add_cluster_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("cluster_id", nargs="?", help="Cluster ID from config (positional)")
    parser.add_argument("--clusterid", dest="clusterid", help="Cluster ID from config (named override)")
    parser.add_argument("--existing-cluster-id", help="Existing Scylla Cloud cluster ID")
    parser.add_argument("--cluster-name", help="Cluster name")
    parser.add_argument("--cluster-type", choices=["x-cloud", "scylla-cloud"], help="Cluster type")
    parser.add_argument("--cloud", choices=["aws", "gcp"], help="Cloud provider")
    parser.add_argument("--region", help="Cloud region")
    parser.add_argument("--scylla-version", help="Scylla version")
    parser.add_argument("--api-interface", choices=["CQL", "ALTERNATOR"], help="User API interface")
    parser.add_argument("--replication-factor", type=int, help="Replication factor")
    parser.add_argument("--broadcast-type", choices=["PRIVATE", "PUBLIC"], help="Cluster broadcast type")
    parser.add_argument("--cidr-block", help="Cluster CIDR block")
    parser.add_argument("--cloud-provider-id", type=int, help="Explicit cloudProviderId override")
    parser.add_argument("--region-id", type=int, help="Explicit regionId override")

    parser.add_argument("--instance-families", help="Comma list, e.g. i8g,i4i")
    parser.add_argument("--instance-types", help="Comma list, e.g. i8g.4xlarge,i8g.8xlarge")
    parser.add_argument("--instance-type-ids", help="Comma list of instance IDs")
    parser.add_argument("--storage-min-gb", type=int, help="X-cloud storage policy min in GB")
    parser.add_argument("--storage-target-utilization", type=float, help="X-cloud storage target utilization (80 or 0.8)")
    parser.add_argument("--vcpu-min", type=int, help="X-cloud vCPU policy minimum")

    parser.add_argument("--wanted-size", "--wantedsize", dest="wanted_size", help="Shortcut override for desired instance size")
    parser.add_argument("--wanted-count", "--wantedcount", dest="wanted_count", type=int, help="Shortcut override for desired node count")
    parser.add_argument("--node-type", help="Scylla-cloud node type override")
    parser.add_argument("--node-type-id", type=int, help="Scylla-cloud instanceId override")
    parser.add_argument("--node-count", type=int, help="Scylla-cloud node count override")


def _add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="Wait for async request completion")
    parser.add_argument("--wait-timeout", type=int, default=3600, help="Max wait time in seconds")
    parser.add_argument("--poll-interval", type=int, default=20, help="Polling interval in seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scm", description="Scylla Cloud Manager (SAF-style CLI)")
    p.add_argument("--config", help="Path to variables.yml/config.yml", default=None)

    sub = p.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Create or attach a managed cluster (SAF style: scm setup x1)")
    _add_cluster_override_args(p_setup)
    _add_common_runtime_args(p_setup)
    _add_wait_args(p_setup)
    p_setup.add_argument("--dry-run", action="store_true", help="Print payload only")
    p_setup.add_argument("--write-back", action="store_true", help="Persist returned cluster ID to existing_cluster_id")
    p_setup.set_defaults(func=cmd_setup)

    p_resize = sub.add_parser("resize", help="Resize or update scaling for a cluster")
    _add_cluster_override_args(p_resize)
    _add_common_runtime_args(p_resize)
    _add_wait_args(p_resize)
    p_resize.add_argument("--dry-run", action="store_true", help="Print payload only")
    p_resize.set_defaults(func=cmd_resize)

    p_destroy = sub.add_parser("destroy", help="Destroy a cluster")
    _add_cluster_override_args(p_destroy)
    _add_common_runtime_args(p_destroy)
    _add_wait_args(p_destroy)
    p_destroy.add_argument("--yes", action="store_true", help="Confirm destructive action")
    p_destroy.add_argument("--dry-run", action="store_true", help="Show target without API call")
    p_destroy.set_defaults(func=cmd_destroy)

    p_status = sub.add_parser("status", help="Show cluster status")
    _add_cluster_override_args(p_status)
    _add_common_runtime_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="List all account clusters")
    _add_common_runtime_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_cache = sub.add_parser("cache-refresh-cloud", help="Refresh cloud-data.json from Scylla Cloud deployment API")
    _add_common_runtime_args(p_cache)
    p_cache.set_defaults(func=cmd_cache_refresh_cloud)

    p_cloud_data = sub.add_parser("cloud-data", help="Show mapped cloud instance data for a region")
    _add_common_runtime_args(p_cloud_data)
    p_cloud_data.add_argument("--cloud", required=True, choices=["aws", "gcp"], help="Cloud provider")
    p_cloud_data.add_argument("--region", required=True, help="Region name, e.g. us-west-2")
    p_cloud_data.add_argument("--families-only", action="store_true", help="Show only instance families")
    p_cloud_data.set_defaults(func=cmd_cloud_data)

    p_validate = sub.add_parser("validate", help="Validate config and mapping resolution")
    _add_common_runtime_args(p_validate)
    p_validate.add_argument("--clusterid", dest="clusterid", help="Single cluster ID to validate")
    p_validate.add_argument("--cluster-type", choices=["x-cloud", "scylla-cloud"], help="Override cluster type")
    p_validate.add_argument("--cloud", choices=["aws", "gcp"], help="Override cloud")
    p_validate.add_argument("--region", help="Override region")
    p_validate.add_argument("--cloud-provider-id", type=int, help="Override cloudProviderId")
    p_validate.add_argument("--region-id", type=int, help="Override regionId")
    p_validate.add_argument("cluster_ids", nargs="*", help="Optional subset of cluster IDs")
    p_validate.set_defaults(func=cmd_validate)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ConfigError, MappingError, APIError) as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()
