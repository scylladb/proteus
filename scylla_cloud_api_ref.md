# Scylla Cloud X-Cloud API Reference

This guide covers automating the X-Cloud cluster lifecycle through the Scylla Cloud API.

## What This Covers

- Fetch existing clusters (Scylla Cloud and X-Cloud)
- Create an X-Cloud cluster
- Define and update scaling policy (scale up/down)
- Track provisioning and scaling progress
- Decommission an X-Cloud cluster

## Requirements

All API requests require an API token issued by Scylla Cloud.

1. Sign in to Scylla Cloud.
2. Open Settings -> Personal Tokens -> Generate.
3. Create a token and store it securely. You will not be able to view it again after closing the page.

Export your token:

```bash
export SC_TOKEN="Your-API-Token"
```

## Base URL and Headers

Base URL:

```text
https://api.cloud.scylladb.com
```

Required headers:

- Authorization: Bearer <Your-API-Token>
- Content-Type: application/json
- Trace-Id: <optional-trace-id> (example: my-automation-v1)

Example header setup:

```bash
export SC_TOKEN="Your-API-Token"

curl -H "Authorization: Bearer $SC_TOKEN" \
     -H "Content-Type: application/json" \
     -H "Trace-Id: my-script" \
     https://api.cloud.scylladb.com/account/default
```

## Account Operations

### Get Account ID

Get the Scylla Cloud account ID associated with your API token.

Endpoint:

```text
GET /account/default
```

Request:

```bash
curl -X GET https://api.cloud.scylladb.com/account/default \
  -H "Authorization: Bearer $SC_TOKEN" \
  -H "Content-Type: application/json"
```

Response (example):

```json
{
  "data": {
    "accountId": 110632,
    "userId": 110139,
    "name": "ScyllaDB",
    "pricingPlan": ""
  }
}
```

Extract account ID:

```bash
ACCOUNT_ID=$(curl -s https://api.cloud.scylladb.com/account/default \
  -H "Authorization: Bearer $SC_TOKEN" | jq -r '.data.accountId')

echo "Your Account ID: $ACCOUNT_ID"
```

From this point on, examples assume ACCOUNT_ID is set.

## Cluster Operations

### List Clusters

List all clusters in your account.

Endpoint:

```text
GET /account/{accountId}/clusters
```

Request:

```bash
curl -X GET "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/clusters" \
  -H "Authorization: Bearer $SC_TOKEN"
```

Response (example):

```json
{
  "data": {
    "clusters": [
      {
        "id": 54321,
        "clusterName": "prod-cluster",
        "scyllaVersion": "2026.1.0",
        "status": "ACTIVE",
        "replicationFactor": 3,
        "broadcastType": "PRIVATE",
        "createdAt": "2025-03-15T10:30:00Z",
        "grafanaUrl": "https://prod-cluster.grafana.cloud.scylladb.com"
      }
    ]
  }
}
```

### Get Cluster Details

Get details for a specific cluster.

Endpoint:

```text
GET /account/{accountId}/cluster/{clusterId}
```

Request:

```bash
CLUSTER_ID=54321

curl -X GET "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/$CLUSTER_ID" \
  -H "Authorization: Bearer $SC_TOKEN"
```

Response (example, truncated):

```json
{
  "data": {
    "cluster": {
      "id": 54321,
      "clusterName": "prod-cluster",
      "scyllaVersion": "2025.3.0",
      "status": "ACTIVE",
      "replicationFactor": 3,
      "broadcastType": "PRIVATE",
      "userApiInterface": "PRIVATE",
      "grafanaUrl": "https://prod-cluster.grafana.cloud.scylladb.com",
      "createdAt": "2025-03-15T10:30:00Z",
      "dataCenters": [
        {
          "id": 101,
          "name": "us-east-1a",
          "status": "ACTIVE",
          "cidrBlock": "10.0.0.0/24",
          "nodeCount": 3,
          "replicationFactor": 3
        }
      ]
    }
  }
}
```

### Create a New X-Cloud Cluster

Create a new X-Cloud cluster.

Endpoint:

```text
POST /account/{accountId}/cluster
```

Request body template:

```json
{
  "accountCredentialId": 7,
  "broadcastType": "PRIVATE",
  "cidrBlock": "10.0.0.0/24",
  "rackCIDRSize": 26,
  "cloudProviderId": 1,
  "regionId": 42,
  "clusterName": "my-xcloud-cluster",
  "replicationFactor": 3,
  "scyllaVersion": "2026.1.0",
  "userApiInterface": "CQL",
  "freeTier": false,
  "tablets": "enforced",
  "availabilityZoneIdsOverride": ["usw2-az1", "usw2-az1", "usw2-az1"],
  "placement": "true",
  "scaling": {
    "instanceFamilies": ["i8g"],
    "instanceTypeIDs": [],
    "mode": "xcloud",
    "policies": {
      "storage": {
        "min": 1024,
        "targetUtilization": 0.8
      },
      "vcpu": {
        "min": 6
      }
    }
  },
  "vectorSearch": {
    "nodeCount": 2,
    "defaultInstanceTypeId": 413
  }
}
```

Key arguments:

- ACCOUNT_ID: Scylla Cloud account ID (from GET /account/default).
- accountCredentialId: Linked cloud credential (AWS IAM role, GCP service account, etc.). Fetch from GET /account/{accountId}/cloud-account.
- broadcastType: PRIVATE or PUBLIC. PRIVATE is typical for private networking.
- cloudProviderId: 1 = AWS, 2 = GCP.
- regionId: Numeric cloud region ID. Fetch via GET /deployment/cloud-provider/{id}/regions.
- scyllaVersion: Target Scylla version. Discover with GET /deployment/scylla-versions.
- tablets: Use enforced for X-Cloud.
- availabilityZoneIdsOverride: Optional. Forces node VMs onto specific AZs. AWS = AZ IDs (e.g. usw2-az1); GCP = zone names (e.g. us-west2-b). Fetch valid IDs via GET /account/{accountId}/cloud-account/{cloudAccountId}/region/{regionId}/zones. For single-AZ, list the same AZ ID replicationFactor times; for multi-AZ, list one ID per AZ to span.
- placement: Optional "true" | "false". Set "true" whenever availabilityZoneIdsOverride is used; required for single-AZ (all nodes in one AZ).
- scaling.instanceFamilies: Allowed family list, usually one family for X-Cloud (for example i8g, i7i).
- scaling.instanceTypeIDs: If empty, X-Cloud picks cost/perf-suitable sizes in the chosen family.
- scaling.mode: xcloud.
- scaling.policies.storage.min: Minimum storage in GiB (for example 1024 = 1 TiB).
- scaling.policies.storage.targetUtilization: Disk utilization threshold before scale out (up to 0.9).
- scaling.policies.vcpu.min: Target minimum vCPU.
- vectorSearch: Optional vector search nodes.

Get vector-search instance types:

```bash
curl -X GET "https://api.cloud.scylladb.com/deployment/cloud-provider/$PROVIDER_ID/region/$REGION_ID?target=VECTOR_SEARCH" \
  -H "Authorization: Bearer $SC_TOKEN"
```

Complete create example:

```bash
# Step 0: Set API token
export SC_TOKEN="Your-API-Token"

# Step 1: Get account ID
ACCOUNT_ID=$(curl -s "https://api.cloud.scylladb.com/account/default" \
  -H "Authorization: Bearer $SC_TOKEN" | jq -r '.data.accountId')

# Step 2: Pick cloud provider (AWS=1, GCP=2)
PROVIDER_ID=1

# Step 3: Get cloud credential ID
CREDENTIAL_ID=$(curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cloud-account" \
  -H "Authorization: Bearer $SC_TOKEN" | \
  jq --argjson pid $PROVIDER_ID '.data[] | select(.cloudProviderId == $pid) | .id' | head -1)

# Step 4: Resolve region ID from region name
REGION_ID=$(curl -s "https://api.cloud.scylladb.com/deployment/cloud-provider/$PROVIDER_ID/regions" \
  -H "Authorization: Bearer $SC_TOKEN" | \
  jq '.data.regions[] | select(.externalId == "us-west-2") | .id')

# Step 5: Create the cluster
curl -X POST "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster" \
  -H "Authorization: Bearer $SC_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"accountCredentialId\": $CREDENTIAL_ID,
    \"broadcastType\": \"PRIVATE\",
    \"cidrBlock\": \"10.0.0.0/24\",
    \"rackCIDRSize\": 26,
    \"cloudProviderId\": $PROVIDER_ID,
    \"regionId\": $REGION_ID,
    \"clusterName\": \"my-xcloud-cluster\",
    \"replicationFactor\": 3,
    \"scyllaVersion\": \"2026.1.0\",
    \"userApiInterface\": \"CQL\",
    \"freeTier\": false,
    \"tablets\": \"enforced\",
    \"scaling\": {
      \"instanceFamilies\": [\"i8g\"],
      \"instanceTypeIDs\": [],
      \"mode\": \"xcloud\",
      \"policies\": {
        \"storage\": { \"min\": 1024, \"targetUtilization\": 0.9 },
        \"vcpu\": { \"min\": 6 }
      }
    }
  }"
```

Create response (example):

```json
{
  "data": {
    "requestId": 228422,
    "fields": {
      "clusterName": "my-xcloud-cluster"
    }
  }
}
```

### Track Cluster Progress

Track in-progress operations such as creation, scaling, and upgrades.

Endpoint:

```text
GET /account/{accountId}/cluster/request/{requestId}
```

Request:

```bash
curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/request/228422" \
  -H "Authorization: Bearer $SC_TOKEN"
```

Response (example):

```json
{
  "Request ID": 228422,
  "Type": "CREATE_CLUSTER",
  "Status": "COMPLETED",
  "Progress": "100%",
  "Description": "The cluster is ready",
  "Cluster ID": 47716,
  "Provisioning": "dedicated-vm",
  "Error": "none"
}
```

The response contains the resulting cluster ID. Status COMPLETED confirms completion.

### Get Nodes

Get cluster nodes, optionally enriched with topology and status.

Endpoint:

```text
GET /account/{accountId}/cluster/{clusterId}/nodes?enriched=true
```

Request:

```bash
curl -X GET "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/47716/nodes?enriched=true" \
  -H "Authorization: Bearer $SC_TOKEN" | jq .
```

Response (example, truncated):

```json
{
  "data": {
    "nodes": [
      {
        "id": 291426,
        "azName": "us-west-2c",
        "azId": "usw2-az3",
        "rackName": "usw2-az3",
        "cloudProviderId": 1,
        "instanceId": 182,
        "regionId": 3,
        "dcId": 46873
      }
    ]
  }
}
```

## Scaling Operations

Update the scaling policy for a cluster data center.

Endpoint:

```text
PUT /account/{accountId}/cluster/{clusterId}/dc/{dcId}/scaling
```

Request body:

```json
{
  "instanceFamilies": ["i8g"],
  "instanceTypeIDs": [],
  "mode": "xcloud",
  "policies": {
    "storage": {
      "min": 1024,
      "targetUtilization": 0.8
    },
    "vcpu": {
      "min": 30
    }
  }
}
```

This example keeps the i8g family, raises vCPU target from 6 to 30, and sets disk target utilization to 80%.

Complete scaling example:

```bash
CLUSTER_ID=47716

# Get DC ID
DC_ID=$(curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/$CLUSTER_ID/dcs" \
  -H "Authorization: Bearer $SC_TOKEN" | jq '.data.dataCenters[0].id')

# Apply scaling policy
curl -X PUT "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/$CLUSTER_ID/dc/$DC_ID/scaling" \
  -H "Authorization: Bearer $SC_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"instanceFamilies\": [\"i8g\"],
    \"instanceTypeIDs\": [],
    \"mode\": \"xcloud\",
    \"policies\": {
      \"storage\": { \"min\": 1024, \"targetUtilization\": 0.8 },
      \"vcpu\": { \"min\": 30 }
    }
  }"
```

Response (example):

```json
{
  "data": {
    "ID": 228430,
    "RequestType": "UPDATE_DC_SCALING"
  }
}
```

Monitor scaling request:

```bash
# requestId returned by scaling call
curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/request/228430" \
  -H "Authorization: Bearer $SC_TOKEN"

# Find resize requests and status
curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/$CLUSTER_ID/request" \
  -H "Authorization: Bearer $SC_TOKEN" | \
  jq '[.data[] | select((.requestType | startswith("RESIZE_CLUSTER"))) | {id, requestType, status, progressPercent, progressDescription}]'
```

Response (example):

```json
[
  {
    "id": 228431,
    "requestType": "RESIZE_CLUSTER_V3",
    "status": "COMPLETED",
    "progressPercent": 100,
    "progressDescription": "resize finished successfully: 3 nodes added, 0 nodes removed"
  }
]
```

## Delete Cluster

Delete a cluster. This action cannot be undone.

Endpoint:

```text
POST /account/{accountId}/cluster/{clusterId}/delete
```

Request body:

```json
{
  "clusterName": "my-test-cluster"
}
```

Note: clusterName is a safety confirmation and must exactly match the display name.

Request:

```bash
CLUSTER_ID=47716
CLUSTER_NAME="my-xcloud-cluster"

curl -X POST "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/$CLUSTER_ID/delete" \
  -H "Authorization: Bearer $SC_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"clusterName\": \"$CLUSTER_NAME\"}"
```

Response (example):

```json
{
  "data": {
    "ID": 228433,
    "RequestType": "DELETE_CLUSTER",
    "AccountID": 110632
  }
}
```

Monitor delete progress:

```bash
curl -s "https://api.cloud.scylladb.com/account/$ACCOUNT_ID/cluster/request/228433" \
  -H "Authorization: Bearer $SC_TOKEN"
```

Expected status transitions from IN_PROGRESS to COMPLETED with progressPercent reaching 100.

## Reference

Official API docs:

https://cloud.docs.scylladb.com/stable/api.html
