from __future__ import annotations

from typing import Any
import requests


class APIError(Exception):
    pass


class ScyllaCloudAPI:
    def __init__(self, token: str, timeout: int = 300, ssl_verify: bool = True) -> None:
        self.base_url = "https://api.cloud.scylladb.com"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self.verify = ssl_verify

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, timeout=self.timeout, verify=self.verify, **kwargs)
        if resp.status_code >= 400:
            raise APIError(f"HTTP {resp.status_code}: {resp.text}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise APIError(f"Non-JSON response from API: {resp.text}") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def get_account_default(self) -> dict[str, Any]:
        return self._request("GET", "/account/default")

    def get_cloud_accounts(self, account_id: int) -> dict[str, Any]:
        return self._request("GET", f"/account/{account_id}/cloud-account")

    def get_cloud_providers(self) -> dict[str, Any]:
        return self._request("GET", "/deployment/cloud-providers")

    def get_provider_regions(self, cloud_provider_id: int, defaults: bool = False) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/deployment/cloud-provider/{cloud_provider_id}/regions",
            params={"defaults": str(defaults).lower()},
        )

    def get_instances_for_region(
        self,
        cloud_provider_id: int,
        region_id: int,
        defaults: bool = False,
        target: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"defaults": str(defaults).lower()}
        if target:
            params["target"] = target
        return self._request(
            "GET",
            f"/deployment/cloud-provider/{cloud_provider_id}/region/{region_id}",
            params=params,
        )

    def list_clusters(self, account_id: int, enriched: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/account/{account_id}/clusters", params={"enriched": str(enriched).lower()})

    def get_cluster(self, account_id: int, cluster_id: int, enriched: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/account/{account_id}/cluster/{cluster_id}", params={"enriched": str(enriched).lower()})

    def get_cluster_dcs(self, account_id: int, cluster_id: int, enriched: bool = True) -> dict[str, Any]:
        return self._request("GET", f"/account/{account_id}/cluster/{cluster_id}/dcs", params={"enriched": str(enriched).lower()})

    def get_cluster_nodes(self, account_id: int, cluster_id: int, enriched: bool = True) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/account/{account_id}/cluster/{cluster_id}/nodes",
            params={"enriched": str(enriched).lower()},
        )

    def get_cluster_request(self, account_id: int, request_id: int) -> dict[str, Any]:
        return self._request("GET", f"/account/{account_id}/cluster/request/{request_id}")

    def list_cluster_requests(self, account_id: int, cluster_id: int, req_type: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if req_type:
            params["type"] = req_type
        if status:
            params["status"] = status
        resp = self._request("GET", f"/account/{account_id}/cluster/{cluster_id}/request", params=params)
        data = resp.get("data", [])
        return data if isinstance(data, list) else []

    def create_cluster(self, account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/account/{account_id}/cluster", json=payload)

    def resize_cluster(self, account_id: int, cluster_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/account/{account_id}/cluster/{cluster_id}/resize", json=payload)

    def update_dc_scaling(self, account_id: int, cluster_id: int, dc_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/account/{account_id}/cluster/{cluster_id}/dc/{dc_id}/scaling", json=payload)

    def delete_cluster(self, account_id: int, cluster_id: int, cluster_name: str) -> dict[str, Any]:
        return self._request("POST", f"/account/{account_id}/cluster/{cluster_id}/delete", json={"clusterName": cluster_name})
