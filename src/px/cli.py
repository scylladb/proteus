from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .api import APIError, ScyllaCloudAPI
from .config import ConfigError, get_cluster, load_config, resolve_config_path, write_back_cluster_field, write_back_cluster_fields
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
    merged = _merge_cluster_overrides(base, args)

    # Backward-compatible defaults: allow SSH key paths at global api level.
    api_cfg = conf.get("api") or {}
    for key in ("ssh_key_public", "ssh_key_private"):
        if not merged.get(key) and api_cfg.get(key):
            merged[key] = api_cfg.get(key)

    return cluster_ref, merged


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

    # If provider_id is set but not numeric, treat it as a lookup failure and fall back
    if provider_id is not None:
        try:
            provider_id = int(provider_id)
        except (TypeError, ValueError):
            provider_id = None
    
    # If region_id is set but not numeric, treat it as a lookup failure and fall back
    if region_id is not None:
        try:
            region_id = int(region_id)
        except (TypeError, ValueError):
            region_id = None

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
    for key in ("requestId", "id", "ID", "RequestId"):
        if data.get(key) is not None:
            try:
                return int(data[key])
            except (TypeError, ValueError):
                pass
    fields = data.get("fields") or {}
    for key in ("requestId", "id", "ID", "RequestId"):
        if fields.get(key) is not None:
            try:
                return int(fields[key])
            except (TypeError, ValueError):
                pass
    return None


def _extract_cluster_id_from_payload(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    for key in ("clusterId", "clusterID", "id"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass

    nested = payload.get("cluster")
    candidate = _extract_cluster_id_from_payload(nested)
    if candidate is not None:
        return candidate

    data = payload.get("data")
    candidate = _extract_cluster_id_from_payload(data)
    if candidate is not None:
        return candidate

    fields = payload.get("fields")
    candidate = _extract_cluster_id_from_payload(fields)
    if candidate is not None:
        return candidate

    clusters = payload.get("clusters")
    if isinstance(clusters, list):
        for item in clusters:
            candidate = _extract_cluster_id_from_payload(item)
            if candidate is not None:
                return candidate

    return None


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _parse_ts(data: dict[str, Any], *keys: str) -> datetime | None:
    """Parse the first found ISO 8601 timestamp from data by trying each key in order."""
    for key in keys:
        raw = data.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _request_elapsed_offset(data: dict[str, Any]) -> float:
    """Return how many seconds the request has already been running, using only server
    timestamps so local clock skew cannot affect the result.

    Strategy:
      1. Prefer updatedAt - createdAt  (both server-side, skew-proof).
      2. Fall back to now - createdAt  (NTP skew is negligible in practice).
      3. Return 0.0 if no usable timestamps found.
    """
    created = _parse_ts(data,
        "createdAt", "created_at", "CreatedAt",
        "submittedAt", "submitted_at", "SubmittedAt",
        "requestTime", "RequestTime",
    )
    updated = _parse_ts(data,
        "updatedAt", "updated_at", "UpdatedAt",
        "lastUpdatedAt", "last_updated_at", "LastUpdatedAt",
        "modifiedAt", "modified_at", "ModifiedAt",
    )
    if created is not None and updated is not None:
        delta = (updated - created).total_seconds()
        if delta > 0:
            return delta
    if created is not None:
        delta = (datetime.now(timezone.utc) - created).total_seconds()
        return max(0.0, delta)
    return 0.0


# ---------------------------------------------------------------------------
# Request cache — persists submitted_at timestamps for --nowait operations
# so `ptx progress` can show accurate elapsed time from submission
# ---------------------------------------------------------------------------

_REQUEST_CACHE_TTL_DAYS = 7


def _request_cache_path(config_path: Path | None) -> Path:
    if config_path is not None:
        return config_path.parent / ".px_requests.json"
    return Path.home() / ".config" / "proteus" / ".px_requests.json"


def _load_request_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {"_version": 1, "requests": {}}
    try:
        data = json.loads(cache_path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("requests"), dict):
            return {"_version": 1, "requests": {}}
        return data
    except Exception:
        return {"_version": 1, "requests": {}}


def _save_request_cache(cache_path: Path, data: dict[str, Any]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_REQUEST_CACHE_TTL_DAYS)
    pruned = {}
    for rid, entry in (data.get("requests") or {}).items():
        try:
            ts = datetime.fromisoformat(str(entry.get("submitted_at", "")).replace("Z", "+00:00"))
            if ts >= cutoff:
                pruned[rid] = entry
        except Exception:
            pass  # drop malformed entries
    data["requests"] = pruned
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass  # best-effort; never crash on cache write


def _record_request(
    config_path: Path | None,
    request_id: int,
    cluster_ref: str,
    cluster_id: int | None,
    operation: str,
    submitted_at: datetime | None = None,
) -> None:
    cache_path = _request_cache_path(config_path)
    data = _load_request_cache(cache_path)
    data["requests"][str(request_id)] = {
        "submitted_at": (submitted_at or datetime.now(timezone.utc)).isoformat(),
        "cluster_ref": cluster_ref,
        "cluster_id": cluster_id,
        "operation": operation,
    }
    _save_request_cache(cache_path, data)


def _record_request_completed(config_path: Path | None, request_id: int) -> None:
    """Stamp completed_at on a cache entry the first time completion is observed."""
    cache_path = _request_cache_path(config_path)
    data = _load_request_cache(cache_path)
    entry = (data.get("requests") or {}).get(str(request_id))
    if entry is not None and "completed_at" not in entry:
        entry["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_request_cache(cache_path, data)


def _update_cache_entry(config_path: Path | None, request_id: int, fields: dict[str, Any]) -> None:
    """Update arbitrary fields on an existing cache entry. No-op if entry not found."""
    cache_path = _request_cache_path(config_path)
    data = _load_request_cache(cache_path)
    entry = (data.get("requests") or {}).get(str(request_id))
    if entry is not None:
        entry.update(fields)
        _save_request_cache(cache_path, data)


def _get_cached_submitted_at(config_path: Path | None, request_id: int) -> datetime | None:
    cache_path = _request_cache_path(config_path)
    entry = (_load_request_cache(cache_path).get("requests") or {}).get(str(request_id))
    if not entry:
        return None
    try:
        return datetime.fromisoformat(str(entry["submitted_at"]).replace("Z", "+00:00"))
    except Exception:
        return None


def _wait_for_request(
    api: ScyllaCloudAPI,
    account_id: int,
    request_id: int,
    timeout_seconds: int,
    poll_interval_seconds: int,
    elapsed_offset: float = 0.0,
    config_path: Path | None = None,
) -> dict[str, Any]:
    start = time.monotonic() - elapsed_offset
    deadline = start + timeout_seconds
    last_desc = ""
    while True:
        resp = api.get_cluster_request(account_id, request_id)
        data = resp.get("data") or {}
        status = str(data.get("status") or data.get("Status") or "UNKNOWN").upper()
        pct = data.get("progressPercent") if data.get("progressPercent") is not None else data.get("ProgressPercent", 0)
        desc = data.get("progressDescription") or data.get("ProgressDescription") or ""

        elapsed = _fmt_elapsed(time.monotonic() - start)
        if status in ("COMPLETED", "DONE", "SUCCEEDED"):
            line = f"  [{status}] {pct}%" + (f" — {desc}" if desc != last_desc else "")
        else:
            line = f"  [{elapsed}] [{status}] {pct}%" + (f" — {desc}" if desc != last_desc else "")
        print(line, flush=True)
        last_desc = desc

        # Keep cache up to date: stamp completed_at on first 100% observation;
        # otherwise update last_seen_active_at so we know when it was last active.
        if config_path is not None:
            if pct == 100 or status in ("COMPLETED", "DONE", "SUCCEEDED"):
                _record_request_completed(config_path, request_id)
            else:
                _update_cache_entry(config_path, request_id, {
                    "last_seen_active_at": datetime.now(timezone.utc).isoformat(),
                })

        if status in ("COMPLETED", "DONE", "SUCCEEDED"):
            return resp
        if status in ("FAILED", "ERROR", "CANCELED"):
            _die(f"Request {request_id} failed: {json.dumps(resp, indent=2)}")

        if time.monotonic() >= deadline:
            _die(f"Timed out waiting for request {request_id}")

        time.sleep(max(1, poll_interval_seconds))


def _wait_for_scale_request(
    api: ScyllaCloudAPI,
    account_id: int,
    cluster_id: int,
    poll_interval_seconds: int,
    trigger_timeout: int = 120,
    config_path: Path | None = None,
) -> None:
    """Phase 2: wait for Scylla Cloud to start a RESIZE_CLUSTER_* request and poll it to completion."""
    poll_start = time.monotonic()
    print("Waiting for Scylla Cloud to start scaling...")
    deadline = poll_start + trigger_timeout
    scale_request_id: int | None = None

    while time.monotonic() < deadline:
        time.sleep(poll_interval_seconds)
        try:
            requests = api.list_cluster_requests(account_id, cluster_id)
        except APIError as exc:
            print(f"  [poll error: {exc}] — retrying")
            continue

        active = [
            r for r in requests
            if (r.get("requestType") or r.get("RequestType") or "").startswith("RESIZE_CLUSTER")
            and (r.get("status") or r.get("Status") or "") in ("QUEUED", "IN_PROGRESS")
        ]
        if active:
            r = active[0]
            scale_request_id = r.get("id") or r.get("ID")
            req_type = r.get("requestType") or r.get("RequestType") or "RESIZE"
            print(f"Resize operation detected [{req_type}] (request {scale_request_id})")
            break

        elapsed_s = time.monotonic() - poll_start
        print(f"  [{_fmt_elapsed(elapsed_s)}] Waiting for scale trigger...")

    if scale_request_id is None:
        print(
            "No active resize operation detected within 2 minutes. "
            "Scylla Cloud will scale automatically when utilization thresholds are breached."
        )
        return

    # Poll the resize request to completion
    last_desc = ""
    while True:
        time.sleep(poll_interval_seconds)
        try:
            resp = api.get_cluster_request(account_id, scale_request_id)
        except APIError as exc:
            print(f"  [poll error: {exc}] — retrying")
            continue
        data = resp.get("data") or {}
        status = str(data.get("status") or data.get("Status") or "UNKNOWN").upper()
        pct = data.get("progressPercent") if data.get("progressPercent") is not None else data.get("ProgressPercent", 0)
        desc = data.get("progressDescription") or data.get("ProgressDescription") or ""

        elapsed = _fmt_elapsed(time.monotonic() - poll_start)
        if status in ("COMPLETED", "DONE", "SUCCEEDED"):
            line = f"  [{status}] {pct}%" + (f" — {desc}" if desc != last_desc else "")
        else:
            line = f"  [{elapsed}] [{status}] {pct}%" + (f" — {desc}" if desc != last_desc else "")
        print(line, flush=True)
        last_desc = desc

        if config_path is not None:
            if pct == 100 or status in ("COMPLETED", "DONE", "SUCCEEDED"):
                _record_request_completed(config_path, scale_request_id)
            else:
                _update_cache_entry(config_path, scale_request_id, {
                    "last_seen_active_at": datetime.now(timezone.utc).isoformat(),
                })

        if status in ("COMPLETED", "DONE", "SUCCEEDED"):
            return
        if status in ("FAILED", "ERROR", "CANCELED"):
            _die(f"Resize request {scale_request_id} failed: {json.dumps(resp, indent=2)}")


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

        # Only resolve to IDs if user explicitly provided instance_type_ids
        # Otherwise, let API use instanceFamilies
        if not ids and names:
            ids = resolve_instance_ids(cloud_data, cloud, region, names)

        storage = scaling.get("storage") or {}
        target = storage.get("target_utilization", 80)
        target = float(target) / 100.0 if float(target) > 1 else float(target)

        scaling_payload: dict[str, Any] = {
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
        
        # Include only one of: instanceFamilies or instanceTypeIDs (API requirement)
        if ids:
            scaling_payload["instanceTypeIDs"] = ids
        elif families:
            scaling_payload["instanceFamilies"] = families
        else:
            raise ConfigError(f"{cluster_id}: scaling requires either instance_families or instance_types/instance_type_ids")
        
        payload["scaling"] = scaling_payload
        # X-Cloud with scaling requires tablets mode to be "enforced"
        payload["tablets"] = "enforced"
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

        # Only resolve to IDs if user explicitly provided instance_type_ids
        # Otherwise, let API use instanceFamilies
        if not ids and names:
            ids = resolve_instance_ids(cloud_data, cloud, region, names)

        storage = scaling.get("storage") or {}
        target = storage.get("target_utilization", 80)
        target = float(target) / 100.0 if float(target) > 1 else float(target)

        payload: dict[str, Any] = {
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
        
        # Include only one of: instanceFamilies or instanceTypeIDs (API requirement)
        if ids:
            payload["instanceTypeIDs"] = ids
        elif families:
            payload["instanceFamilies"] = families
        else:
            raise ConfigError("scaling requires either instance_families or instance_types/instance_type_ids")
        
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
                    "wantedSize": node_count,
                    "instanceTypeId": int(instance_id),
                }
            ]
        }
        return "scylla-cloud", payload

    raise ConfigError("cluster_type must be x-cloud or scylla-cloud")


def _print_json(title: str, payload: dict[str, Any]) -> None:
    print(title)
    print(json.dumps(payload, indent=2, sort_keys=False))


def _extract_cluster_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("clusters", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _cluster_status_value(cluster_obj: dict[str, Any]) -> str:
    for key in ("clusterStatus", "status", "state"):
        value = cluster_obj.get(key)
        if value not in (None, ""):
            return str(value)
    status_info = cluster_obj.get("statusInfo") or {}
    if isinstance(status_info, dict):
        value = status_info.get("status")
        if value not in (None, ""):
            return str(value)
    return "UNKNOWN"


def _cluster_name_value(cluster_obj: dict[str, Any]) -> str:
    for key in ("clusterName", "name"):
        value = cluster_obj.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _extract_datacenter_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("dataCenters", "datacenters", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _extract_node_items(dc: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("nodes", "instances", "servers", "items", "dcNodes"):
        value = dc.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _extract_cluster_node_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("nodes", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _node_status_value(node: dict[str, Any]) -> str:
    for key in ("status", "state", "nodeStatus", "instanceStatus"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return "UNKNOWN"


def _node_name_value(node: dict[str, Any]) -> str:
    for key in ("name", "hostId", "id", "instanceId"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return "-"


def _node_type_value(node: dict[str, Any]) -> str:
    for key in ("instanceType", "instanceTypeName", "nodeType", "machineType"):
        value = node.get(key)
        if value not in (None, ""):
            return str(value)
    return "-"


def _node_ip_value(node: dict[str, Any]) -> str:
    private_ip = node.get("privateIp") or node.get("privateIPAddress") or node.get("privateAddress")
    public_ip = node.get("publicIp") or node.get("publicIPAddress") or node.get("publicAddress")
    if private_ip and public_ip:
        return f"private={private_ip}, public={public_ip}"
    if private_ip:
        return f"private={private_ip}"
    if public_ip:
        return f"public={public_ip}"
    return "-"


def _dc_name_value(dc: dict[str, Any]) -> str:
    for key in ("name", "dcName", "regionName", "id"):
        value = dc.get(key)
        if value not in (None, ""):
            return str(value)
    return "-"


def _dc_status_value(dc: dict[str, Any]) -> str:
    for key in ("status", "state", "dcStatus"):
        value = dc.get(key)
        if value not in (None, ""):
            return str(value)
    return "UNKNOWN"


def _cloud_env_value(cluster_obj: dict[str, Any]) -> str:
    cloud = (
        cluster_obj.get("cloudProviderFullName")
        or ((cluster_obj.get("cloudProvider") or {}).get("name") if isinstance(cluster_obj.get("cloudProvider"), dict) else None)
        or cluster_obj.get("cloud")
        or "-"
    )
    region_raw = cluster_obj.get("regionName") or cluster_obj.get("region") or "-"
    if isinstance(region_raw, dict):
        region = region_raw.get("fullName") or region_raw.get("externalId") or region_raw.get("name") or "-"
    else:
        region = str(region_raw)
    return f"{cloud} / {region}"


_SYM_UP   = "[✔]"  # green  – fully active
_SYM_DOWN = "[✘]"  # red    – failed / error
_SYM_PART = "[◑]"  # yellow – transitioning / partial
_SYM_NONE = "[–]"  # dim    – not provisioned


def _status_icon(status: Any) -> str:
    value = str(status or "").strip().upper()
    if value in ("", "–", "-", "UNKNOWN"):
        return _SYM_NONE
    if value in ("ACTIVE", "COMPLETED", "DONE", "SUCCEEDED", "RUNNING", "READY", "UP"):
        return _SYM_UP
    if value in ("CREATING", "UPDATING", "SCALING", "RESIZING", "MAINTENANCE", "PENDING", "QUEUED",
                 "DELETING", "TERMINATING", "STOPPING", "CANCELING", "CANCELLING"):
        return _SYM_PART
    return _SYM_DOWN


def _status_badge(status: Any) -> str:
    value = str(status or "UNKNOWN").upper()
    if value in ("ACTIVE", "COMPLETED", "DONE", "SUCCEEDED", "RUNNING", "READY", "UP"):
        return f"{_SYM_UP} {value}"
    if value in ("CREATING", "UPDATING", "SCALING", "RESIZING", "MAINTENANCE", "PENDING", "QUEUED",
                 "DELETING", "TERMINATING", "STOPPING", "CANCELING", "CANCELLING"):
        return f"{_SYM_PART} {value}"
    return f"{_SYM_DOWN} {value}"


def _to_gib(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{(float(value) / 1024.0):.1f}"


def _to_gbps(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{(float(value) / 1024.0):.1f}"


def _node_region_label(node: dict[str, Any]) -> str:
    region = node.get("region")
    if isinstance(region, dict):
        return str(region.get("dcName") or region.get("name") or region.get("id") or "-")
    return str(region or "-")


def _node_instance_meta(node: dict[str, Any]) -> dict[str, Any]:
    instance = node.get("instance")
    return instance if isinstance(instance, dict) else {}


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["(no node rows returned)"]

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))

    tl, tr, bl, br = "╭", "╮", "╰", "╯"
    hz, vt = "─", "│"
    jt, jb, jl, jr, jc = "┬", "┴", "├", "┤", "┼"

    def hline(left: str, join: str, right: str) -> str:
        return left + join.join(hz * (w + 2) for w in widths) + right

    out = [hline(tl, jt, tr)]
    out.append(vt + vt.join(f" {headers[i].ljust(widths[i])} " for i in range(len(headers))) + vt)
    out.append(hline(jl, jc, jr))
    for row in rows:
        out.append(vt + vt.join(f" {str(row[i]).ljust(widths[i])} " for i in range(len(headers))) + vt)
    out.append(hline(bl, jb, br))
    return out


def _cluster_summary_lines(ref: str, cluster_id: str, cluster_data: dict[str, Any]) -> list[str]:
    lines = [
        f"ref: {ref}",
        f"cluster_id: {cluster_id}",
        f"provisioned: {_SYM_UP} yes",
        f"name: {_cluster_name_value(cluster_data) or '-'}",
        f"status: {_status_badge(_cluster_status_value(cluster_data))}",
        f"cloud_env: {_cloud_env_value(cluster_data)}",
    ]

    cluster_type = cluster_data.get("clusterType") or cluster_data.get("type")
    region_raw = cluster_data.get("regionName") or cluster_data.get("region")
    if isinstance(region_raw, dict):
        region = region_raw.get("fullName") or region_raw.get("externalId") or region_raw.get("name")
    else:
        region = region_raw
    version_raw = cluster_data.get("scyllaVersion") or cluster_data.get("version")
    if isinstance(version_raw, dict):
        version = version_raw.get("version") or version_raw.get("versionId")
    else:
        version = version_raw

    if cluster_type not in (None, ""):
        lines.append(f"type: {cluster_type}")
    if region not in (None, ""):
        lines.append(f"region: {region}")
    if version not in (None, ""):
        lines.append(f"scylla_version: {version}")
    return lines


def _extract_node_groups(cluster_data: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract node groups from cluster ring status.
    
    Returns list of (group_name, instance_info) tuples where instance_info may be:
    - "instance_type" for single instance
    - "count × instance_type" for multiple instances
    - "" for node groups without instance info
    """
    groups: list[tuple[str, str]] = []
    
    # Try to extract from ringStatus or nodeRings
    ring_status = cluster_data.get("ringStatus") or cluster_data.get("nodeRings") or []
    if isinstance(ring_status, list):
        for ring in ring_status:
            if isinstance(ring, dict):
                name = ring.get("nodeGroupName") or ring.get("name") or ""
                if name:
                    # Try to get instance type and count
                    instance_type = ring.get("instanceType") or ""
                    node_count = ring.get("nodeCount") or 0
                    
                    if node_count and node_count > 1 and instance_type:
                        instance_info = f"{node_count} × {instance_type}"
                    elif instance_type:
                        instance_info = instance_type
                    else:
                        instance_info = ""
                    
                    groups.append((name, instance_info))
    
    return groups


def _status_box(title: str, lines: list[str], width_limit: int = 120) -> None:
    wrapped: list[str] = []
    for line in lines:
        chunks = textwrap.wrap(str(line), width=width_limit) or [""]
        wrapped.extend(chunks)

    width = max(len(title), *(len(line) for line in wrapped)) if wrapped else len(title)
    border = "╭" + "─" * (width + 2) + "╮"
    divider = "├" + "─" * (width + 2) + "┤"
    bottom = "╰" + "─" * (width + 2) + "╯"

    print(border)
    print(f"│ {title.ljust(width)} │")
    print(divider)
    if wrapped:
        for line in wrapped:
            print(f"│ {line.ljust(width)} │")
    else:
        print(f"│ {'(no details)'.ljust(width)} │")
    print(bottom)


def _print_cluster_status_box(
    api: ScyllaCloudAPI,
    account_id: int,
    ref: str,
    cluster_id: int,
    configured_name: str | None = None,
) -> None:
    cluster_resp = api.get_cluster(account_id, cluster_id, enriched=True)
    cluster_data = cluster_resp.get("data") or {}
    # X-Cloud wraps cluster details under data["cluster"]
    inner = cluster_data.get("cluster") or cluster_data
    title_name = _cluster_name_value(inner) or (configured_name or "-")
    title = f"Cluster {title_name} ({cluster_id})"

    summary_lines = _cluster_summary_lines(ref, str(cluster_id), inner)

    try:
        dcs_resp = api.get_cluster_dcs(account_id, cluster_id, enriched=True)
        dcs = _extract_datacenter_items(dcs_resp)
    except APIError as exc:
        dcs = []
        summary_lines.append(f"datacenters: unavailable ({exc})")

    if dcs:
        summary_lines.append("")
        summary_lines.append("datacenters:")
        for dc in dcs:
            dc_name = _dc_name_value(dc)
            dc_status = _status_badge(_dc_status_value(dc))
            nodes = _extract_node_items(dc)
            summary_lines.append(f"- {dc_name} ({dc_status}), nodes={len(nodes)}")

    try:
        nodes_resp = api.get_cluster_nodes(account_id, cluster_id, enriched=True)
        nodes = _extract_cluster_node_items(nodes_resp)
    except APIError as exc:
        nodes = []
        summary_lines.append(f"\nnodes: unavailable ({exc})")

    # ---- Print header ----------------------------------------------------------
    print(f"  {title}")
    print("  " + "─" * len(title))
    for line in summary_lines:
        print(f"  {line}")

    # ---- Print node table at natural width -------------------------------------
    if nodes:
        print()
        headers = [
            "Private IP",
            "Status",
            "State",
            "DC/Region",
            "Instance Type",
            "Memory (GB)",
            "Storage (GB)",
            "vCPUs",
            "Net (Gbps)",
        ]
        rows: list[list[str]] = []
        for node in nodes:
            instance = _node_instance_meta(node)
            rows.append(
                [
                    str(node.get("privateIp") or node.get("privateIPAddress") or "-"),
                    _status_badge(_node_status_value(node)),
                    str(node.get("state") or "-"),
                    _node_region_label(node),
                    str(instance.get("externalId") or _node_type_value(node) or "-"),
                    _to_gib(instance.get("memory")),
                    str(instance.get("totalStorage") or "-"),
                    str(instance.get("cpuCount") or "-"),
                    _to_gbps(instance.get("networkSpeed")),
                ]
            )
        rows.sort(key=lambda r: (r[4], r[0]))
        for line in _render_table(headers, rows):
            print(line)


def cmd_setup(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

    api_allow_create = conf.get("api", {}).get("allow_create", False)
    if not cluster.get("allow_create", api_allow_create):
        _die(
            f"Cluster '{cluster_ref}': creation is not permitted. "
            "Set 'allow_create: true' under the 'api:' section (applies to all clusters) "
            "to permit cluster creation."
        )

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
    if request_id is not None:
        _record_request(config_path, request_id, cluster_ref, None, "setup")
    cluster_name_target = payload.get("clusterName")
    
    final = None
    if args.wait and request_id is not None:
        print(f"\n⏳ Waiting for cluster '{cluster_name_target}' to be ready...")
        final = _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval, config_path=config_path)
        print(f"✓ Cluster '{cluster_name_target}' is ready!")

    candidate_id = _extract_cluster_id_from_payload(final)
    if candidate_id is None:
        candidate_id = _extract_cluster_id_from_payload(resp.get("data") or {})

    # If no ID in response, try to find cluster by name via list API
    if candidate_id is None:
        try:
            clusters_resp = api.list_clusters(account_id)
            clusters_data = clusters_resp.get("data") or {}
            clusters = clusters_data.get("clusters") if isinstance(clusters_data, dict) else []
            if isinstance(clusters, list):
                for c in clusters:
                    if not isinstance(c, dict):
                        continue
                    if c.get("clusterName") == cluster_name_target:
                        candidate_id = _extract_cluster_id_from_payload(c)
                        if candidate_id is not None:
                            break
        except Exception as e:
            print(f"Warning: Could not auto-discover cluster ID: {e}")

    if candidate_id is not None and config_path is not None:
        try:
            write_back_cluster_field(config_path, cluster_ref, "existing_cluster_id", int(candidate_id))
            print(f"✓ Updated config with existing_cluster_id={candidate_id}")
        except Exception as e:
            print(f"Warning: Could not update config: {e}")
    elif candidate_id is not None and config_path is None:
        print(f"Cluster created with ID: {candidate_id} (config file not available to auto-save)")
    elif candidate_id is None:
        print(f"Cluster creation initiated (requestId={request_id}). Please wait for completion and set existing_cluster_id manually.")


def cmd_resize(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

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
    if request_id is not None:
        _record_request(config_path, request_id, cluster_ref, cluster_id, "resize")
    cluster_name = str(cluster.get("cluster_name") or cluster_id)

    if args.wait and request_id is not None:
        if mode == "x-cloud":
            # Phase 1: wait for the policy update request to complete
            print(f"\n⏳ Applying scaling policy for '{cluster_name}'...")
            _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval, config_path=config_path)
            print(f"✓ Scaling policy saved.")
            # Phase 2: watch for the auto-scaler to start a RESIZE_CLUSTER_* request
            _wait_for_scale_request(api, account_id, cluster_id, args.poll_interval, config_path=config_path)
            print(f"✓ Cluster '{cluster_name}' scaling complete!")
        else:
            print(f"\n⏳ Waiting for cluster '{cluster_name}' resize to complete...")
            _wait_for_request(api, account_id, request_id, args.wait_timeout, args.poll_interval)
            print(f"✓ Cluster '{cluster_name}' resize complete!")
    elif request_id is not None:
        print(f"Resize submitted (requestId={request_id}). Use --wait to poll for completion.")


def _confirm(prompt: str) -> bool:
    """Prompt user for yes/no confirmation. Returns True if user confirms."""
    while True:
        try:
            response = input(f"{prompt} (yes/no): ").strip().lower()
        except EOFError:
            # No input available (e.g., pipe or redirect) - default to no
            print("(no input provided, cancelling)")
            return False
        
        if response in ("yes", "y"):
            return True
        if response in ("no", "n"):
            return False
        print("Please enter 'yes' or 'no'")


def cmd_destroy(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

    api_allow_destroy = conf.get("api", {}).get("allow_destroy", False)
    if not cluster.get("allow_destroy", api_allow_destroy):
        _die(
            f"Cluster '{cluster_ref}': destruction is not permitted. "
            "Set 'allow_destroy: true' under the 'api:' section (applies to all clusters) "
            "or under the specific cluster to permit deletion."
        )

    # Prompt for confirmation if --yes not provided
    if not args.yes and not args.dry_run:
        cluster_name = str(cluster.get("cluster_name") or "").strip()
        if not _confirm(f"Are you sure you want to destroy cluster '{cluster_name}'?"):
            print("Destroy cancelled.")
            return
        # Second factor: require the user to type the cluster name exactly
        try:
            typed = input(f"Type the cluster name to confirm: ").strip()
        except EOFError:
            typed = ""
        if typed != cluster_name:
            print(f"Name mismatch (expected '{cluster_name}'). Destroy cancelled.")
            return

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
    if request_id is not None:
        _record_request(config_path, request_id, cluster_ref, cluster_id, "destroy")
        print(f"\nDestroy submitted (requestId={request_id}).")
        print(f"  Track deletion: ptx progress {cluster_ref} --follow")
    else:
        print(f"\nDestroy request submitted.")

    if config_path is not None and cluster_ref in (conf.get("clusters") or {}):
        try:
            write_back_cluster_field(config_path, cluster_ref, "existing_cluster_id", None)
            print(f"✓ Cleared existing_cluster_id for '{cluster_ref}' in config")
        except Exception as e:
            print(f"Warning: Could not clear existing_cluster_id: {e}")


def _render_unified_status_table(clusters: list[dict[str, Any]]) -> None:
    """Render all clusters in a unified multi-column table with node groups as sub-rows."""
    if not clusters:
        print("No clusters to display.")
        return
    
    # Prepare table data
    rows: list[list[list[str]]] = []  # Each row is a list of lines (for multi-line cells)
    
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id", "-"))
        cluster_name = str(cluster.get("cluster_name", "-"))
        cloud_id = cluster.get("cloud_id")
        scylla_version = str(cluster.get("scylla_version", "-"))
        cloud_env = str(cluster.get("cloud_env", "-"))
        node_groups = cluster.get("node_groups", [])
        cluster_status = cluster.get("cluster_status", "UNKNOWN")
        icon = _status_icon(cluster_status)

        # cluster_name cell: name on first line, cloud ID as sub-line if provisioned
        name_lines = [cluster_name]
        if cloud_id is not None:
            name_lines.append(f"↳ id:{cloud_id}")

        # Build status lines (node groups)
        status_lines = []
        for group_name, instance_info in node_groups:
            if instance_info:
                status_lines.append(f"{icon} {group_name:<20}{instance_info}")
            else:
                status_lines.append(f"{icon} {group_name}")

        if not status_lines:
            status_lines = [f"{icon} (no node groups)"]

        num_lines = max(len(name_lines), len(status_lines))

        # Create row with all lines for this cluster
        row = [
            [cluster_id] + [""] * (num_lines - 1),
            name_lines + [""] * (num_lines - len(name_lines)),
            [scylla_version] + [""] * (num_lines - 1),
            [cloud_env] + [""] * (num_lines - 1),
            status_lines + [""] * (num_lines - len(status_lines)),
        ]
        rows.append(row)
    
    # Calculate column widths
    col_widths = [
        max(len("cluster_id"), *(len(str(r[0][0])) for r in rows)),
        max(len("cluster_name"), *(max(len(s) for s in r[1]) if r[1] else 0 for r in rows)),
        max(len("scylla_version"), *(len(str(r[2][0])) for r in rows)),
        max(len("cloud"), *(len(str(r[3][0])) for r in rows)),
        max(len("status"), *(max(len(s) for s in r[4]) if r[4] else 0 for r in rows)),
    ]
    
    # Box drawing characters
    tl, tr, bl, br = "╭", "╮", "╰", "╯"
    hz, vt = "─", "│"
    jt, jb, jl, jr, jc = "┬", "┴", "├", "┤", "┼"
    
    def hline(left: str, join: str, right: str) -> str:
        return left + join.join(hz * (w + 2) for w in col_widths) + right
    
    # Print table
    print(hline(tl, jt, tr))
    
    # Header row
    headers = ["cluster_id", "cluster_name", "scylla_version", "cloud", "status"]
    print(vt + vt.join(f" {headers[i].ljust(col_widths[i])} " for i in range(len(headers))) + vt)
    print(hline(jl, jc, jr))
    
    # Data rows
    for row_idx, row in enumerate(rows):
        num_lines = len(row[4])
        for line_idx in range(num_lines):
            cells = [
                row[0][min(line_idx, len(row[0]) - 1)],
                row[1][min(line_idx, len(row[1]) - 1)],
                row[2][min(line_idx, len(row[2]) - 1)],
                row[3][min(line_idx, len(row[3]) - 1)],
                row[4][line_idx] if line_idx < len(row[4]) else "",
            ]
            print(vt + vt.join(f" {str(cells[i]).ljust(col_widths[i])} " for i in range(len(cells))) + vt)
        
        # Separator between clusters (but not after the last one)
        if row_idx < len(rows) - 1:
            print(hline(jl, jc, jr))
    
    print(hline(bl, jb, br))


def _extract_placeholder_node_groups(cluster_config: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract placeholder node groups from config for unprovisions clusters."""
    groups: list[tuple[str, str]] = []
    
    # Try to use node_groups from config (for scylla-cloud)
    node_groups_config = cluster_config.get("node_groups")
    if isinstance(node_groups_config, list):
        for ng in node_groups_config:
            if isinstance(ng, dict):
                name = ng.get("name") or ""
                node_type = ng.get("node_type") or ""
                count = ng.get("count") or 0
                
                if name:
                    if count and count > 1 and node_type:
                        instance_info = f"{count} × {node_type}"
                    elif node_type:
                        instance_info = node_type
                    else:
                        instance_info = ""
                    groups.append((name, instance_info))
    
    # If no node_groups in config, show placeholder
    if not groups:
        groups.append(("scylla", ""))
    
    return groups


# ---------------------------------------------------------------------------
# Helpers for cmd_sync
# ---------------------------------------------------------------------------

def _normalize_cloud(raw: str | None) -> str | None:
    """Map API cloud provider names to config values 'aws' or 'gcp'."""
    if not raw:
        return None
    lower = raw.lower()
    if "amazon" in lower or lower == "aws":
        return "aws"
    if "google" in lower or lower == "gcp":
        return "gcp"
    return raw.lower()


def _normalize_cluster_type(raw: str | None) -> str | None:
    """Map API clusterType values to config values 'x-cloud' or 'scylla-cloud'."""
    if not raw:
        return None
    lower = raw.lower().replace("_", "-")
    if "xcloud" in lower or lower == "x-cloud":
        return "x-cloud"
    if "scylla" in lower or "standard" in lower:
        return "scylla-cloud"
    return lower


def _extract_region_name(raw: Any) -> str | None:
    """Return a region external ID string from whatever the enriched API sends."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return (
            raw.get("externalId")
            or raw.get("fullName")
            or raw.get("name")
        )
    return str(raw)


def cmd_sync(args: argparse.Namespace) -> None:
    """Populate a sparse cluster config entry from live API data."""
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

    cluster_id = cluster.get("existing_cluster_id")
    if not cluster_id:
        _die(f"Cluster '{cluster_ref}' has no existing_cluster_id — nothing to sync from")
    cluster_id = int(cluster_id)

    token, timeout, ssl_verify = _api_settings(conf, args)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)

    resp = api.get_cluster(account_id, cluster_id, enriched=True)
    raw = resp.get("data") or {}
    # X-Cloud wraps cluster details under data["cluster"]
    inner = raw.get("cluster") or raw

    # ---- Determine cluster type ------------------------------------------------
    # Prefer scalingMode field (present on all enriched clusters) over clusterType
    scaling_mode = str(inner.get("scalingMode") or "").lower()
    if scaling_mode == "xcloud":
        cluster_type = "x-cloud"
    else:
        cluster_type = _normalize_cluster_type(
            inner.get("clusterType") or inner.get("type")
        ) or "scylla-cloud"

    # ---- Build field map -------------------------------------------------------
    fields: dict[str, Any] = {}

    name = _cluster_name_value(inner)
    if name:
        fields["cluster_name"] = name

    fields["cluster_type"] = cluster_type

    # Cloud provider
    cloud_raw = (
        (inner.get("cloudProvider") or {}).get("name")
        or inner.get("cloudProviderFullName")
        or inner.get("cloud")
    )
    cloud = _normalize_cloud(cloud_raw)
    if cloud:
        fields["cloud"] = cloud

    # Region — prefer externalId from enriched dict
    region = _extract_region_name(
        inner.get("regionName") or inner.get("region")
    )
    if region:
        fields["region"] = region

    # ScyllaDB version
    version_raw = inner.get("scyllaVersion") or inner.get("version")
    version = (
        version_raw.get("version") or version_raw.get("versionId")
        if isinstance(version_raw, dict)
        else version_raw
    )
    if version:
        fields["scylla_version"] = str(version)

    # API interface
    iface = inner.get("userApiInterface") or inner.get("apiInterface")
    if iface:
        fields["api_interface"] = str(iface).upper()

    # Network
    bcast = inner.get("broadcastType")
    if bcast:
        fields["broadcast_type"] = str(bcast).upper()

    # CIDR — cluster level, then embedded dc dict, then dataCenters list, then vpcList
    cidr = inner.get("cidrBlock") or inner.get("networkAddress")
    if not cidr:
        embedded_dc = inner.get("dc") or {}
        cidr = embedded_dc.get("cidrBlock")
    if not cidr:
        dcs_inline = inner.get("dataCenters") or []
        if dcs_inline:
            cidr = (dcs_inline[0] or {}).get("cidrBlock")
    if not cidr:
        vpc_list = inner.get("vpcList") or []
        if vpc_list:
            cidr = (vpc_list[0] or {}).get("cidrBlock")
    if cidr:
        fields["cidr_block"] = str(cidr)

    rf = inner.get("replicationFactor") or inner.get("replication_factor")
    if rf:
        fields["replication_factor"] = int(rf)

    # ---- Type-specific fields --------------------------------------------------
    if cluster_type == "scylla-cloud":
        # Derive node_groups from embedded instance + nodes list
        instance = inner.get("instance") or {}
        node_type = instance.get("externalId") or instance.get("name")
        nodes = inner.get("nodes") or []
        node_count = len(nodes) if nodes else None
        if not node_count:
            node_count = int(rf) if rf else None

        # DC name as group name (fall back to "primary")
        embedded_dc = inner.get("dc") or (inner.get("dataCenters") or [{}])[0]
        grp_name = embedded_dc.get("name") or "primary"

        if node_type or node_count:
            entry: dict[str, Any] = {"name": grp_name}
            if node_type:
                entry["node_type"] = str(node_type)
            if node_count:
                entry["count"] = int(node_count)
            fields["node_groups"] = [entry]

    elif cluster_type == "x-cloud":
        # Fetch DC scaling policy
        try:
            dcs_resp = api.get_cluster_dcs(account_id, cluster_id, enriched=True)
            dcs_data = dcs_resp.get("data") or {}
            dcs = (
                dcs_data.get("dataCenters")
                or dcs_data.get("datacenters")
                or (dcs_data if isinstance(dcs_data, list) else [])
            )
            if not dcs:
                # Fall back to inline dataCenters in cluster response
                dcs = inner.get("dataCenters") or []
            if dcs and isinstance(dcs, list):
                dc = dcs[0]
                sp = dc.get("scaling") or dc.get("scalingPolicy") or {}
                policies = sp.get("policies") or {}
                storage_p = policies.get("storage") or {}
                vcpu_p = policies.get("vcpu") or {}

                scaling: dict[str, Any] = {}

                raw_families = sp.get("instanceFamilies") or dc.get("instanceFamilies")
                if raw_families:
                    scaling["instance_families"] = list(raw_families)

                stor: dict[str, Any] = {}
                min_gb = storage_p.get("min")
                if min_gb is not None:
                    stor["min_gb"] = int(min_gb)
                target_util = storage_p.get("targetUtilization")
                if target_util is not None:
                    tv = float(target_util)
                    # API stores as fraction (0.8); config uses percent (80)
                    stor["target_utilization"] = int(round(tv * 100)) if tv <= 1.0 else int(tv)
                if stor:
                    scaling["storage"] = stor

                vcpu_min = vcpu_p.get("min")
                if vcpu_min is not None:
                    scaling["vcpu"] = {"min": int(vcpu_min)}

                if scaling:
                    fields["scaling"] = scaling
        except APIError as exc:
            print(f"Warning: could not fetch DC scaling policy: {exc}")

    # ---- Write back ------------------------------------------------------------
    if not fields:
        print(f"No fields discovered for '{cluster_ref}' (cluster {cluster_id}) — nothing written.")
        return

    if config_path is None:
        print("No config file found — cannot write back. Discovered fields:")
        for k, v in fields.items():
            print(f"  {k}: {v}")
        return

    write_back_cluster_fields(config_path, cluster_ref, fields)

    print(f"Synced '{cluster_ref}' (cluster {cluster_id}) — {len(fields)} field(s) written:")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def cmd_progress(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)
    cluster_ref, cluster = _cluster_from_sources(conf, args)

    token, timeout, ssl_verify = _api_settings(conf, args)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    cluster_id = _cluster_numeric_id(cluster)

    active_request_id: int | None = None
    req_type: str = ""
    active_status: str = ""
    active_req_data: dict[str, Any] = {}
    elapsed_offset: float = 0.0

    # --- Step 1: use local cache to find the request WE submitted ---
    # This avoids picking up stale requests whose API status hasn't updated yet.
    cache_data = _load_request_cache(_request_cache_path(config_path))
    best_cached: tuple[int, datetime, str] | None = None
    for rid_str, entry in (cache_data.get("requests") or {}).items():
        if entry.get("cluster_ref") != cluster_ref:
            continue
        try:
            ts = datetime.fromisoformat(str(entry["submitted_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if best_cached is None or ts > best_cached[1]:
            best_cached = (int(rid_str), ts, entry.get("operation", ""))

    if best_cached is not None:
        cached_rid, cached_ts, _ = best_cached
        try:
            resp = api.get_cluster_request(account_id, cached_rid)
            rd = resp.get("data") or {}
            st = str(rd.get("status") or rd.get("Status") or "").upper()
            rt = str(rd.get("requestType") or rd.get("RequestType") or "")
            pct = rd.get("progressPercent") or rd.get("ProgressPercent") or 0
            if _is_active_request_status(st) and pct != 100:
                active_request_id = cached_rid
                req_type = rt
                active_status = st
                active_req_data = rd
                elapsed_offset = max(0.0, (datetime.now(timezone.utc) - cached_ts).total_seconds())
        except APIError:
            pass

    # --- Step 2: cached request already done — scan for follow-on RESIZE_CLUSTER_* ---
    # Also the fallback when no cache entry exists.
    if active_request_id is None:
        try:
            reqs = api.list_cluster_requests(account_id, cluster_id)
        except APIError as exc:
            _die(f"Could not fetch cluster requests: {exc}")
        for r in reqs:
            st = str(r.get("status") or r.get("Status") or "").upper()
            rt = str(r.get("requestType") or r.get("RequestType") or "")
            pct = r.get("progressPercent") or r.get("ProgressPercent") or 0
            # Skip requests stuck at 100% with stale status (API hasn't caught up)
            if pct == 100:
                continue
            if _is_active_request_status(st) and rt.startswith("RESIZE_CLUSTER"):
                rid = r.get("id") or r.get("ID")
                try:
                    full = (api.get_cluster_request(account_id, rid)).get("data") or {}
                    r = {**r, **full}
                except APIError:
                    pass
                active_request_id = rid
                req_type = rt
                active_status = st
                active_req_data = r
                # Use cache time of the original trigger as total elapsed if available
                if best_cached is not None:
                    elapsed_offset = max(0.0, (datetime.now(timezone.utc) - best_cached[1]).total_seconds())
                    # Record this RESIZE_CLUSTER_* in the cache using the original
                    # trigger's submitted_at so total elapsed is from `ptx resize`.
                    # Add 1ms so it sorts after the trigger and becomes best_cached.
                    resize_submitted_at = best_cached[1] + timedelta(milliseconds=1)
                    _record_request(config_path, rid, cluster_ref, cluster_id, req_type, submitted_at=resize_submitted_at)
                else:
                    elapsed_offset = _request_elapsed_offset(r)
                break

    if active_request_id is None:
        # No active request — show the last known state from cache instead of a bare message
        if best_cached is not None:
            cached_rid, cached_ts, _ = best_cached
            try:
                resp = api.get_cluster_request(account_id, cached_rid)
                rd = resp.get("data") or {}
                st = str(rd.get("status") or rd.get("Status") or "UNKNOWN").upper()
                rt = str(rd.get("requestType") or rd.get("RequestType") or "")
                pct = rd.get("progressPercent") or rd.get("ProgressPercent") or 0
                desc = rd.get("progressDescription") or rd.get("ProgressDescription") or ""
                # Use completed_at recorded in the cache for a stable duration.
                # If not yet stamped (first time seeing it as completed), stamp it now.
                if st not in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
                    _record_request_completed(config_path, cached_rid)
                cache_entry = (_load_request_cache(_request_cache_path(config_path)).get("requests") or {}).get(str(cached_rid)) or {}
                completed_at_str = cache_entry.get("completed_at")
                if completed_at_str:
                    try:
                        completed_at = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
                        duration = max(0.0, (completed_at - cached_ts).total_seconds())
                    except Exception:
                        duration = 0.0
                else:
                    duration = 0.0
                type_label = f" [{rt}]" if rt else ""
                print(f"Last request {cached_rid}{type_label} [{st}] on cluster '{cluster_ref}'")
                line = f"  [{st}] {pct}%" + (f" — {desc}" if desc else "")
                print(line)
                return
            except APIError:
                pass
        print(f"No active request found for cluster '{cluster_ref}' (id=={cluster_id}).")
        return

    elapsed_so_far = _fmt_elapsed(elapsed_offset)
    type_label = f" [{req_type}]" if req_type else ""
    print(f"Active request {active_request_id}{type_label} [{active_status}] on cluster '{cluster_ref}' (id={cluster_id}) — {elapsed_so_far} elapsed")

    if not args.wait:
        pct = active_req_data.get("progressPercent") or active_req_data.get("ProgressPercent") or 0
        desc = active_req_data.get("progressDescription") or active_req_data.get("ProgressDescription") or ""
        line = f"  [{elapsed_so_far}] [{active_status}] {pct}%" + (f" — {desc}" if desc else "")
        # If it's already complete in the snapshot, stamp completed_at
        if active_status in ("COMPLETED", "DONE", "SUCCEEDED"):
            _record_request_completed(config_path, active_request_id)
        print(line)
        return

    # --wait: poll the active request to completion
    try:
        _wait_for_request(api, account_id, active_request_id, args.wait_timeout, args.poll_interval, elapsed_offset=elapsed_offset, config_path=config_path)
    except KeyboardInterrupt:
        print(f"\nDetached from polling. Operation continues in the background.")
        print(f"  Resume with: ptx progress {cluster_ref} --follow")
        return

    # If the completed request was a trigger type (e.g. UPDATE_DC_SCALING), a
    # RESIZE_CLUSTER_* is spawned asynchronously afterwards — follow it too.
    if not req_type.startswith("RESIZE_CLUSTER"):
        print("Watching for resize operation to begin...")
        try:
            _wait_for_scale_request(api, account_id, cluster_id, args.poll_interval, config_path=config_path)
        except KeyboardInterrupt:
            print(f"\nDetached from polling. Operation continues in the background.")
            print(f"  Resume with: ptx progress {cluster_ref} --follow")


def cmd_status(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)

    # Single cluster detail view (requires API token)
    if args.cluster_id or args.clusterid:
        token, timeout, ssl_verify = _api_settings(conf, args)
        api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
        account_id = _account_id(api)
        _, cluster = _cluster_from_sources(conf, args)
        cluster_id = _cluster_numeric_id(cluster)
        configured_name = str(cluster.get("cluster_name") or "")
        _print_cluster_status_box(api, account_id, str(args.cluster_id or args.clusterid), cluster_id, configured_name)
        return

    # Collect cluster summaries from config
    cluster_summaries: list[dict[str, Any]] = []
    configured_clusters = conf.get("clusters") or {}

    if not isinstance(configured_clusters, dict):
        _die("Invalid clusters configuration")

    # Determine if we need API access (any cluster has existing_cluster_id set)
    need_api = any(
        cluster.get("existing_cluster_id") not in (None, "", 0)
        for cluster in configured_clusters.values()
        if isinstance(cluster, dict)
    )
    
    api = None
    account_id = None
    if need_api:
        token, timeout, ssl_verify = _api_settings(conf, args)
        api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
        account_id = _account_id(api)

    for ref, raw_cluster in configured_clusters.items():
        if not isinstance(raw_cluster, dict):
            continue

        cluster_name = str(raw_cluster.get("cluster_name") or "")
        scylla_version = str(raw_cluster.get("scylla_version") or "-")
        
        # Determine cloud environment
        cloud = str(raw_cluster.get("cloud") or "-").upper()
        region = str(raw_cluster.get("region") or "")
        cluster_type = str(raw_cluster.get("cluster_type") or "").lower()
        
        if cluster_type == "x-cloud":
            cloud_env = f"{cloud} - X-Cloud"
        elif cluster_type == "scylla-cloud":
            cloud_env = f"{cloud} - Scylla Cloud"
        else:
            cloud_env = cloud
        
        existing_id = raw_cluster.get("existing_cluster_id")
        
        # Try to fetch from API if provisioned
        node_groups: list[tuple[str, str]] = []
        if existing_id not in (None, "", 0) and api is not None and account_id is not None:
            try:
                cluster_id = int(existing_id)
                cluster_resp = api.get_cluster(account_id, cluster_id, enriched=True)
                cluster_data = cluster_resp.get("data") or {}

                # Get cluster status
                inner = cluster_data.get("cluster") or cluster_data
                cluster_status = _cluster_status_value(inner)

                # Try ringStatus-based extraction first (works for Scylla Cloud)
                node_groups = _extract_node_groups(cluster_data)

                # Fall back: derive groups from live node list
                if not node_groups:
                    nodes_resp = api.get_cluster_nodes(account_id, cluster_id, enriched=True)
                    nodes = _extract_cluster_node_items(nodes_resp)
                    if nodes:
                        # Group by instance type, count per group
                        counts: dict[str, int] = {}
                        for node in nodes:
                            inst = _node_instance_meta(node)
                            itype = inst.get("externalId") or _node_type_value(node)
                            counts[itype] = counts.get(itype, 0) + 1
                        for itype, cnt in counts.items():
                            label = f"{cnt} × {itype}" if cnt > 1 else itype
                            node_groups.append(("nodes", label))

            except (APIError, TypeError, ValueError):
                cluster_status = "UNKNOWN"
                node_groups = _extract_placeholder_node_groups(raw_cluster)
        else:
            cluster_status = "–"
            node_groups = _extract_placeholder_node_groups(raw_cluster)
        
        summary = {
            "cluster_id": ref,
            "cluster_name": cluster_name or "-",
            "cloud_id": int(existing_id) if existing_id not in (None, "", 0) else None,
            "scylla_version": scylla_version,
            "cloud_env": cloud_env,
            "node_groups": node_groups,
            "cluster_status": cluster_status,
        }
        cluster_summaries.append(summary)

    # Render unified table
    _render_unified_status_table(cluster_summaries)



def cmd_list(args: argparse.Namespace) -> None:
    config_path = resolve_config_path(args.config, allow_missing=True)
    conf = load_config(config_path)

    token, timeout, ssl_verify = _api_settings(conf, args)
    api = ScyllaCloudAPI(token=token, timeout=timeout, ssl_verify=ssl_verify)
    account_id = _account_id(api)
    resp = api.list_clusters(account_id, enriched=True)

    if getattr(args, "json", False):
        _print_json("Cloud clusters:", resp)
        return

    clusters = _extract_cluster_items(resp)
    if not clusters:
        print("(no clusters found)")
        return

    headers = ["ID", "Name", "Status", "Cloud / Region", "DCs", "Nodes", "Created"]
    rows: list[list[str]] = []
    for c in clusters:
        cid = str(c.get("id") or c.get("clusterId") or "-")
        name = _cluster_name_value(c) or "-"
        status = _status_badge(_cluster_status_value(c))
        cloud_region = _cloud_env_value(c)
        dcs = c.get("dataCenters") or c.get("datacenters") or []
        dc_count = len(dcs) if isinstance(dcs, list) else 0
        # Sum replicationFactor across DCs — the list endpoint does not inline nodes,
        # but RF equals node count per DC for standard clusters and is the RF target for X-Cloud.
        # Fall back to the embedded single-dc object if dataCenters is empty.
        node_count = 0
        if isinstance(dcs, list) and dcs:
            for dc in dcs:
                if isinstance(dc, dict):
                    rf = dc.get("replicationFactor") or dc.get("replication_factor") or 0
                    inline = _extract_node_items(dc)
                    node_count += len(inline) if inline else int(rf)
        if node_count == 0:
            embedded_dc = c.get("dc") or {}
            rf = embedded_dc.get("replicationFactor") or embedded_dc.get("replication_factor") or 0
            node_count = int(rf) * max(dc_count, 1) if rf else 0
        created = _parse_ts(c, "createdAt", "created_at", "CreatedAt")
        if created is None:
            # createdAt is absent from the list endpoint — fetch from the single-cluster endpoint
            cid_int = c.get("id") or c.get("clusterId")
            if cid_int is not None:
                try:
                    detail_resp = api.get_cluster(account_id, int(cid_int), enriched=False)
                    detail = (detail_resp.get("data") or {}).get("cluster") or (detail_resp.get("data") or {})
                    created = _parse_ts(detail, "createdAt", "created_at", "CreatedAt")
                except APIError:
                    pass
        created_str = created.strftime("%Y-%m-%d") if created else "-"
        rows.append([cid, name, status, cloud_region, str(dc_count), str(node_count), created_str])

    for line in _render_table(headers, rows):
        print(line)


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
    parser.add_argument("--vcpu-min", "--vcpu", dest="vcpu_min", type=int, help="X-cloud vCPU policy minimum")

    parser.add_argument("--wanted-size", "--wantedsize", dest="wanted_size", help="Shortcut override for desired instance size")
    parser.add_argument("--wanted-count", "--wantedcount", dest="wanted_count", type=int, help="Shortcut override for desired node count")
    parser.add_argument("--node-type", help="Scylla-cloud node type override")
    parser.add_argument("--node-type-id", type=int, help="Scylla-cloud instanceId override")
    parser.add_argument("--node-count", type=int, help="Scylla-cloud node count override")


def _add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", dest="wait", action="store_true", default=True, help="Wait for async request completion (default)")
    parser.add_argument("--no-wait", "--nowait", dest="wait", action="store_false", help="Skip waiting for completion")
    parser.add_argument("--wait-timeout", type=int, default=3600, help="Max wait time in seconds")
    parser.add_argument("--poll-interval", type=int, default=20, help="Polling interval in seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ptx", description="Proteus — SAF-style CLI for Scylla Cloud / X-Cloud")
    p.add_argument("--config", help="Path to variables.yml/config.yml", default=None)

    sub = p.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="Create or attach a managed cluster (SAF style: ptx setup x1)")
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
    p_destroy.add_argument("--yes", action="store_true", help="Confirm destructive action")
    p_destroy.add_argument("--dry-run", action="store_true", help="Show target without API call")
    p_destroy.set_defaults(func=cmd_destroy)

    p_progress = sub.add_parser("progress", help="Show or follow active request progress for a cluster")
    _add_cluster_override_args(p_progress)
    _add_common_runtime_args(p_progress)
    p_progress.add_argument("--wait", "--follow", "-f", action="store_true", help="Poll until the request completes (default: one-shot snapshot)")
    p_progress.add_argument("--wait-timeout", type=int, default=3600, help="Max wait time in seconds (used with --wait)")
    p_progress.add_argument("--poll-interval", type=int, default=20, help="Polling interval in seconds (used with --wait)")
    p_progress.set_defaults(func=cmd_progress)

    p_sync = sub.add_parser("sync", help="Populate cluster config from live API data (requires existing_cluster_id)")
    _add_cluster_override_args(p_sync)
    _add_common_runtime_args(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_status = sub.add_parser("status", help="Show cluster status")
    _add_cluster_override_args(p_status)
    _add_common_runtime_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="List all account clusters")
    _add_common_runtime_args(p_list)
    p_list.add_argument("--json", action="store_true", help="Emit raw JSON instead of table")
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
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
