# Configuration Guide

Reference for Proteus's `config.yml`. The CLI is `px`; see [README.md](README.md) for command usage.

---

## Table of Contents

1. [File Discovery](#file-discovery)
2. [File Structure](#file-structure)
3. [`api` — Global Settings](#api--global-settings)
4. [`reference_data` — ID Mapping](#reference_data--id-mapping)
5. [Cluster Definition — Common Fields](#cluster-definition--common-fields)
6. [X-Cloud Clusters](#x-cloud-clusters)
7. [Scylla Cloud Clusters](#scylla-cloud-clusters)
8. [Vector Search](#vector-search)
9. [Local Request Cache](#local-request-cache)
10. [API Error Catalog](#api-error-catalog)
11. [Examples](#examples)

---

## File Discovery

Proteus picks config in this order:

1. `--config <path>` CLI flag
2. `$PROTEUS_CONFIG` env var
3. `~/.config/proteus/config.yml` (default after `install.sh`)
4. Walk up from CWD for a `config.yml` containing `clusters:`

---

## File Structure

```yaml
api:
  token: ...
  timeout: 300
  ssl_verify: true
  ssh_key_public: ...
  ssh_key_private: ...

reference_data:
  cloud_data_path: ./cloud-data.json
  api_error_codes_path: ./api_error_codes.tsv
  auto_refresh: false

clusters:
  <cluster-id>:
    # common fields
    # x-cloud: scaling.*  /  scylla-cloud: node_groups
```

> Sections like `operations:` or `advanced:` in the example config are placeholders for future use and are not consumed by Proteus today. Lifecycle timing is controlled via the `--wait-timeout` and `--poll-interval` CLI flags.

---

## `api` — Global Settings

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `token` | string | Yes | — | Prefer `${SCYLLA_CLOUD_API_TOKEN}` env substitution. Also resolvable via `--api-token` or env. |
| `timeout` | int | No | `300` | Request timeout (seconds). |
| `ssl_verify` | bool | No | `true` | Disable with `--no-ssl-verify` for dev/self-signed. |
| `ssh_key_public` | string | Yes | — | Path to public SSH key sent to the cluster. `~` expands. |
| `ssh_key_private` | string | Yes | — | Path used for direct node access. `~` expands. |
| `allow_create` | bool | No | `false` | Permit `px setup` to create new clusters. Set `true` to enable provisioning. Attaching via `existing_cluster_id` always works regardless of this flag. |
| `allow_destroy` | bool | No | `false` | Permit `px destroy` globally. Can be overridden per-cluster (see below). |

> **Per-cluster `allow_destroy` override:** Add `allow_destroy: true` (or `false`) directly under a cluster entry to override the global `api.allow_destroy` for that cluster only. This lets you protect most clusters while allowing teardown of specific ones.

```yaml
api:
  token: "${SCYLLA_CLOUD_API_TOKEN}"
  timeout: 300
  ssl_verify: true
  ssh_key_public: ~/.ssh/id_ed25519.pub
  ssh_key_private: ~/.ssh/id_ed25519
  allow_create: false   # set true to enable cluster creation
  allow_destroy: false  # set true (or override per-cluster) to enable destroy

---

## `reference_data` — ID Mapping

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `cloud_data_path` | string | `./cloud-data.json` | Provider/region/instance map. Resolved relative to the config file directory. |
| `api_error_codes_path` | string | `./api_error_codes.tsv` | Tab-separated `code\tdescription`. |
| `auto_refresh` | bool | `false` | If true, refresh `cloud-data.json` from the API on startup. |

### `cloud-data.json` schema

```
{
  "instances": {
    "aws|gcp": {
      "<region>": {
        "<instance_type>": {
          "provider_id": <int>,
          "region_id":   <int>,
          "id":          <int>
        }
      }
    }
  }
}
```

### Resolution order

1. Explicit IDs in config (`resolved_ids.cloud_provider_id`, `resolved_ids.region_id`, `node_groups[].node_type_id`, `scaling.instance_type_ids`).
2. Friendly-name lookup in `cloud-data.json` using `cloud`, `region`, `node_type` / instance family.
3. If unresolved, validation fails before any API call.

Inspect mappings: `px cloud-data --cloud aws --region us-west-2`. Refresh: `px cache-refresh-cloud`.

---

## Cluster Definition — Common Fields

Each entry under `clusters:` is keyed by a cluster ID (used positionally on the CLI, e.g. `px setup x1`).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cluster_name` | string | Yes | Display name (lowercase, hyphens, ≤ 63 chars). |
| `description` | string | No | Free text. |
| `cluster_type` | enum | Yes | `x-cloud` or `scylla-cloud`. |
| `existing_cluster_id` | int | No | Numeric Scylla Cloud cluster ID. If set, `setup` attaches instead of creating. Written back automatically after a successful create. |
| `cloud` | enum | Yes | `aws` or `gcp`. |
| `region` | string | Yes | e.g. `us-west-2`, `us-east1`. |
| `scylla_version` | string | Yes | e.g. `2026.1.3`. |
| `api_interface` | enum | No | `CQL` (default) or `ALTERNATOR` (DynamoDB-compatible). |
| `replication_factor` | int | No | Default `3`. |
| `broadcast_type` | enum | Yes | `PRIVATE` (VPC peering) or `PUBLIC`. |
| `cidr_block` | string | Yes | Cluster VPC CIDR. Must not overlap with loader or peer VPCs. |
| `resolved_ids.cloud_provider_id` | int | No | Explicit `cloudProviderId` — skips lookup. |
| `resolved_ids.region_id` | int | No | Explicit `regionId` — skips lookup. |

```yaml
clusters:
  x1:
    cluster_name: prod-xc-aws
    cluster_type: x-cloud
    cloud: aws
    region: us-west-2
    scylla_version: 2026.1.3
    broadcast_type: PRIVATE
    cidr_block: 172.31.0.0/24
```

---

## X-Cloud Clusters

X-Cloud uses auto-scaling policies, not fixed counts.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `scaling.instance_families` | list[str] | Yes | Families auto-scaler may use (e.g. `[i8g, i4i]`). |
| `scaling.instance_types` | list[str] | No | Restrict to specific sizes. Empty = any size in family. |
| `scaling.instance_type_ids` | list[int] | No | Explicit `instanceTypeID`s; overrides family/type lookup. |
| `scaling.storage.min_gb` | int | No | Storage floor in GB. `0` = no floor. |
| `scaling.storage.target_utilization` | int | Yes | % at which scale-up triggers (typically 75–85). |
| `scaling.vcpu.min` | int | Yes | vCPU floor — scale-down stops here. |

```yaml
scaling:
  instance_families: [i8g]
  instance_types: []
  storage:
    min_gb: 1024
    target_utilization: 80
  vcpu:
    min: 12
```

**Resize semantics:** an X-Cloud `resize` updates the scaling policy. Proteus polls the policy update, then waits for the auto-scaler's follow-up `RESIZE_CLUSTER_*` request to complete.

---

## Scylla Cloud Clusters

Standard Scylla Cloud uses fixed node groups.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `node_groups` | list | Yes | One or more node groups. |
| `node_groups[].name` | string | Yes | e.g. `primary`, `analytics`. |
| `node_groups[].node_type` | string | Yes | e.g. `i8g.4xlarge`, `n2-highmem-8`. |
| `node_groups[].node_type_id` | int | No | Explicit Scylla Cloud `instanceId` — skips lookup. |
| `node_groups[].count` | int | Yes | Node count. |

```yaml
node_groups:
  - name: primary
    node_type: i8g.4xlarge
    count: 3
```

Resize via `px resize sc1 --node-count 5` or by editing `count` in config and running `px resize sc1`. `--node-type` changes instance type.

---

## Vector Search

X-Cloud only. Optional vector search nodes attached to the cluster.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vector_search.enabled` | bool | `false` | Attach vector search nodes. |
| `vector_search.count` | int | `2` | Number of vector search nodes. |
| `vector_search.instance_type` | string | `r7g.large` | Instance type. Must exist in region. |

```yaml
vector_search:
  enabled: true
  count: 2
  instance_type: r7g.large
```

---

## Local Request Cache

Proteus writes `.px_requests.json` alongside `config.yml` whenever an async request is submitted (`setup`, `resize`, `destroy`). This file is the only persistent state Proteus maintains beyond the config file.

### Purpose

- **Accurate elapsed timing** in `px progress` — the API does not return an `UpdatedAt` field, so Proteus tracks `submitted_at` locally and stamps `completed_at` on first COMPLETED observation.
- **Request identity** — helps `px progress` find the correct in-flight request when the API's request list contains stale entries.
- **Session resumption** — closing the terminal mid-operation is safe; re-run `px progress <cluster> --follow` to reattach.

### Format

```json
{
  "_version": 1,
  "requests": {
    "<request_id>": {
      "submitted_at": "<ISO 8601 UTC>",
      "cluster_ref": "<cluster key from config>",
      "cluster_id": 49505,
      "operation": "resize | destroy | setup | RESIZE_CLUSTER_V3 | ...",
      "completed_at": "<ISO 8601 UTC>"   // stamped on first COMPLETED observation
    }
  }
}
```

### Maintenance

- Entries older than **7 days** are pruned automatically on every write.
- The file is safe to delete; Proteus recreates it on the next operation. Elapsed timing for any pending request will fall back to `now − API createdAt`.
- The file is not version-controlled by default (add `.px_requests.json` to `.gitignore`).

---

## API Error Catalog

`api_error_codes.tsv` is `code\tdescription`. On API failure, Proteus extracts the code and prints both code and human-readable description. Unknown codes show the raw response.

Sample entries:

- `040703` — Cluster name already used
- `040713` — CIDR range overlaps a reserved network
- `000001` — Too Many Requests

---

## Examples

### 1. New X-Cloud cluster on AWS

```yaml
api:
  token: "${SCYLLA_CLOUD_API_TOKEN}"
  ssh_key_public: ~/.ssh/id_ed25519.pub
  ssh_key_private: ~/.ssh/id_ed25519

clusters:
  x1:
    cluster_name: prod-xc
    cluster_type: x-cloud
    cloud: aws
    region: us-west-2
    scylla_version: 2026.1.3
    broadcast_type: PRIVATE
    cidr_block: 172.31.0.0/24
    scaling:
      instance_families: [i8g]
      storage:
        min_gb: 1024
        target_utilization: 80
      vcpu:
        min: 12
```

```bash
px setup x1 --dry-run
px setup x1
```

### 2. Scylla Cloud on GCP with multiple node groups

```yaml
clusters:
  prod-gcp:
    cluster_name: prod-gcp
    cluster_type: scylla-cloud
    cloud: gcp
    region: us-central1
    scylla_version: 2026.1.3
    api_interface: CQL
    broadcast_type: PRIVATE
    cidr_block: 172.32.0.0/24
    node_groups:
      - name: primary
        node_type: n2-highmem-8
        count: 3
```

### 3. Attach to and resize an existing X-Cloud cluster

```yaml
clusters:
  legacy:
    cluster_name: legacy
    cluster_type: x-cloud
    existing_cluster_id: 12345
    cloud: aws
    region: eu-west-1
    scaling:
      instance_families: [i8g, i4i]
      storage:
        target_utilization: 75
      vcpu:
        min: 16
```

```bash
px status legacy
px resize legacy
```

---

## Validation

```bash
px validate            # all clusters
px validate x1 sc1     # specific clusters
```

Checks: required fields present, friendly names resolve via `cloud-data.json`, IDs available for API submission. Validate runs offline — no live API calls.

---

## Best Practices

- **Secrets** — Keep `SCYLLA_CLOUD_API_TOKEN` in your shell rc, not in the config file.
- **CIDRs** — Plan non-overlapping `/24`s across clusters and peers.
- **Broadcast** — `PRIVATE` for production; `PUBLIC` only for throwaway dev.
- **Instance families** — Stick to one family per cluster where possible (`i8g` on AWS, `n2` family on GCP).
- **vCPU floor** — Set `scaling.vcpu.min` deliberately; it's the only thing preventing pathological scale-down.
