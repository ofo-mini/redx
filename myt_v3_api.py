#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener


DEFAULT_MYT_V3_HOST_API_PORT = 8000
DEFAULT_TIMEOUT_SECONDS = 10.0


class MytV3ApiError(RuntimeError):
    pass


def normalize_host_api_base_url(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError("empty MYT V3 host api url")
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.hostname:
        raise ValueError(f"invalid MYT V3 host api url: {value!r}")
    scheme = parsed.scheme or "http"
    port = parsed.port or DEFAULT_MYT_V3_HOST_API_PORT
    return f"{scheme}://{parsed.hostname}:{port}"


def host_api_hostname(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.hostname:
        raise ValueError(f"invalid MYT V3 host api base url: {base_url!r}")
    return parsed.hostname


@dataclass(frozen=True)
class MytV3ResolvedTarget:
    host_api_base_url: str
    host_ip: str
    host_info: dict[str, Any]
    host_device_info: dict[str, Any]
    container: dict[str, Any]
    adb_target: str
    myt_http_endpoint: str
    myt_rpa_endpoint: str

    def as_record(self) -> dict[str, Any]:
        return {
            "host_api_base_url": self.host_api_base_url,
            "host_ip": self.host_ip,
            "host_info": self.host_info,
            "host_device_info": self.host_device_info,
            "container": self.container,
            "adb_target": self.adb_target,
            "myt_http_endpoint": self.myt_http_endpoint,
            "myt_rpa_endpoint": self.myt_rpa_endpoint,
        }


class MytV3HostApi:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = normalize_host_api_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}))

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        with self._opener.open(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8", "replace")
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MytV3ApiError(f"invalid JSON from {url}: {payload!r}") from exc
        if not isinstance(body, dict):
            raise MytV3ApiError(f"unexpected JSON payload from {url}: {body!r}")
        return body

    def get_info(self) -> dict[str, Any]:
        return self._request_json("/info")

    def get_device_info(self) -> dict[str, Any]:
        return self._request_json("/info/device")

    def list_android(self) -> dict[str, Any]:
        return self._request_json("/android")

    def container_status_record(self, container: dict[str, Any]) -> dict[str, Any]:
        host_ip = host_api_hostname(self.base_url)

        def endpoint(internal_port: int) -> str | None:
            try:
                return self._resolve_endpoint(container, host_ip=host_ip, internal_port=internal_port)
            except MytV3ApiError:
                return None

        return {
            "host_api_base_url": self.base_url,
            "host_ip": host_ip,
            "container_name": container.get("name"),
            "container_index": self._safe_int(container.get("indexNum")),
            "container_status": container.get("status"),
            "adb_target": endpoint(5555),
            "myt_http_endpoint": endpoint(9082),
            "myt_rpa_endpoint": endpoint(9083),
            "container_id": container.get("id") or container.get("containerId") or container.get("ID"),
            "image_url": container.get("imageUrl") or container.get("image") or container.get("imageName"),
            "model_path": container.get("modelPath") or container.get("LocalModel") or container.get("localModel"),
            "data_path": container.get("dataPath"),
            "network_name": container.get("networkName"),
            "container_ip": container.get("ip"),
            "raw_payload": container,
        }

    def list_local_phone_models(self) -> dict[str, Any]:
        return self._request_json("/phoneModel")

    def list_android_phone_models(self) -> dict[str, Any]:
        return self._request_json("/android/phoneModel")

    def create_android(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("/android", method="POST", payload=payload)

    def delete_android(self, name: str) -> dict[str, Any]:
        return self._request_json(f"/android?{urlencode({'name': name})}", method="DELETE")

    def start_android(self, name: str) -> dict[str, Any]:
        return self._request_json("/android/start", method="POST", payload={"name": name})

    def stop_android(self, name: str) -> dict[str, Any]:
        return self._request_json("/android/stop", method="POST", payload={"name": name})

    def restart_android(self, name: str) -> dict[str, Any]:
        return self._request_json("/android/restart", method="POST", payload={"name": name})

    def copy_android(self, *, name: str, index_num: int | None = None, count: int | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"name": name}
        if index_num is not None:
            query["indexNum"] = index_num
        if count is not None:
            query["count"] = count
        return self._request_json(f"/android/copy?{urlencode(query)}")

    def list_phone_model_backups(self) -> dict[str, Any]:
        return self._request_json("/android/backup/model")

    def save_phone_model_backup(self, *, name: str, suffix: str) -> dict[str, Any]:
        return self._request_json(
            "/android/backup/model",
            method="POST",
            payload={"name": name, "suffix": suffix},
        )

    def delete_phone_model_backup(self, name: str) -> dict[str, Any]:
        return self._request_json(f"/android/backup/model?{urlencode({'name': name})}", method="DELETE")

    def add_vpc_socks(self, *, alias: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request_json(
            "/mytVpc/socks",
            method="POST",
            payload={
                "alias": alias,
                "list": items,
            },
        )

    def get_vpc_group(self, *, alias: str) -> dict[str, Any]:
        return self._request_json(f"/mytVpc/group?{urlencode({'alias': alias})}")

    def add_vpc_rule(self, *, name: str, vpc_id: int) -> dict[str, Any]:
        return self._request_json(
            "/mytVpc/addRule",
            method="POST",
            payload={
                "name": name,
                "vpcID": vpc_id,
            },
        )

    def delete_vpc_rule(self, *, name: str) -> dict[str, Any]:
        return self._request_json(
            "/mytVpc/delRule",
            method="POST",
            payload={"name": name},
        )

    def list_vpc_rules(self) -> dict[str, Any]:
        return self._request_json("/mytVpc/containerRule")

    def find_vpc_node_id(self, *, alias: str) -> int:
        payload = self.get_vpc_group(alias=alias)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MytV3ApiError(f"unexpected VPC group payload: {payload!r}")
        groups = data.get("list")
        if not isinstance(groups, list) or not groups:
            raise MytV3ApiError(f"VPC group not found: {alias}")
        for group in groups:
            if not isinstance(group, dict) or str(group.get("alias") or "") != alias:
                continue
            vpcs = group.get("vpcs")
            if not isinstance(vpcs, dict):
                continue
            items = vpcs.get("list")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                vpc_id = self._safe_int(item.get("id"))
                if vpc_id is not None:
                    return vpc_id
        raise MytV3ApiError(f"VPC node id not found for alias: {alias}")

    def list_running_targets(
        self,
        *,
        container_names: set[str] | None = None,
        container_indices: set[int] | None = None,
        max_targets: int | None = None,
    ) -> list[MytV3ResolvedTarget]:
        host_info = self.get_info()
        device_info_payload = self.get_device_info()
        android_payload = self.list_android()
        device_info = device_info_payload.get("data")
        if not isinstance(device_info, dict):
            raise MytV3ApiError(f"unexpected device info payload: {device_info_payload!r}")
        android_data = android_payload.get("data")
        if not isinstance(android_data, dict):
            raise MytV3ApiError(f"unexpected android payload: {android_payload!r}")
        container_list = android_data.get("list")
        if not isinstance(container_list, list):
            raise MytV3ApiError(f"unexpected container list payload: {android_payload!r}")

        running = [
            item
            for item in container_list
            if isinstance(item, dict) and str(item.get("status", "")).lower() == "running"
        ]
        if container_names:
            running = [item for item in running if str(item.get("name", "")) in container_names]
        if container_indices:
            running = [
                item
                for item in running
                if self._safe_int(item.get("indexNum")) in container_indices
            ]
        running.sort(key=lambda item: (self._safe_int(item.get("indexNum")) or 10_000, str(item.get("name", ""))))
        if max_targets is not None and max_targets >= 0:
            running = running[:max_targets]

        host_ip = host_api_hostname(self.base_url)
        return [
            self._resolve_target_from_container(
                container,
                host_info=host_info,
                device_info=device_info,
                host_ip=host_ip,
            )
            for container in running
        ]

    def resolve_target(
        self,
        *,
        container_name: str | None = None,
        container_index: int | None = None,
    ) -> MytV3ResolvedTarget:
        host_info = self.get_info()
        device_info_payload = self.get_device_info()
        android_payload = self.list_android()
        device_info = device_info_payload.get("data")
        if not isinstance(device_info, dict):
            raise MytV3ApiError(f"unexpected device info payload: {device_info_payload!r}")
        android_data = android_payload.get("data")
        if not isinstance(android_data, dict):
            raise MytV3ApiError(f"unexpected android payload: {android_payload!r}")
        container_list = android_data.get("list")
        if not isinstance(container_list, list):
            raise MytV3ApiError(f"unexpected container list payload: {android_payload!r}")

        running = [
            item
            for item in container_list
            if isinstance(item, dict) and str(item.get("status", "")).lower() == "running"
        ]
        if container_name:
            running = [item for item in running if str(item.get("name", "")) == container_name]
        if container_index is not None:
            running = [item for item in running if self._safe_int(item.get("indexNum")) == container_index]
        if not running:
            raise MytV3ApiError("no running V3 container matched the requested selection")
        running.sort(key=lambda item: (self._safe_int(item.get("indexNum")) or 10_000, str(item.get("name", ""))))
        container = running[0]

        host_ip = host_api_hostname(self.base_url)
        return self._resolve_target_from_container(
            container,
            host_info=host_info,
            device_info=device_info,
            host_ip=host_ip,
        )

    def _resolve_target_from_container(
        self,
        container: dict[str, Any],
        *,
        host_info: dict[str, Any],
        device_info: dict[str, Any],
        host_ip: str,
    ) -> MytV3ResolvedTarget:
        adb_target = self._resolve_endpoint(container, host_ip=host_ip, internal_port=5555)
        myt_http_endpoint = self._resolve_endpoint(container, host_ip=host_ip, internal_port=9082)
        myt_rpa_endpoint = self._resolve_endpoint(container, host_ip=host_ip, internal_port=9083)
        return MytV3ResolvedTarget(
            host_api_base_url=self.base_url,
            host_ip=host_ip,
            host_info=host_info,
            host_device_info=device_info,
            container=container,
            adb_target=adb_target,
            myt_http_endpoint=myt_http_endpoint,
            myt_rpa_endpoint=myt_rpa_endpoint,
        )

    def _resolve_endpoint(
        self,
        container: dict[str, Any],
        *,
        host_ip: str,
        internal_port: int,
    ) -> str:
        bound_port = self._lookup_bound_port(container.get("portBindings"), internal_port)
        if bound_port is not None:
            return f"{host_ip}:{bound_port}"

        container_ip = str(container.get("ip") or "").strip()
        network_name = str(container.get("networkName") or "").strip().lower()
        if container_ip and network_name and network_name != "bridge":
            return f"{container_ip}:{internal_port}"

        index_num = int(container.get("indexNum", 0) or 0)
        if index_num > 0:
            base_port = 30000 + (index_num - 1) * 100
            offset_map = {
                5555: 0,
                9082: 1,
                9083: 2,
            }
            offset = offset_map.get(internal_port)
            if offset is not None:
                return f"{host_ip}:{base_port + offset}"
        raise MytV3ApiError(f"unable to resolve endpoint for internal port {internal_port}")

    @staticmethod
    def _lookup_bound_port(port_bindings: Any, internal_port: int) -> int | None:
        if not isinstance(port_bindings, dict):
            return None
        candidates = [
            f"{internal_port}/tcp",
            f"{internal_port}/udp",
        ]
        for key in candidates:
            entries = port_bindings.get(key)
            if not isinstance(entries, list) or not entries:
                continue
            first = entries[0]
            if not isinstance(first, dict):
                continue
            host_port = str(first.get("HostPort") or "").strip()
            if host_port.isdigit():
                return int(host_port)
        return None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
