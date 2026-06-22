#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import ProxyHandler
from urllib.request import build_opener

DEFAULT_MYT_HTTP_ENDPOINT = os.environ.get("MYT_HTTP_ENDPOINT")
DEFAULT_MYT_HTTP_HOST = os.environ.get("MYT_HTTP_HOST", "192.168.50.148")
DEFAULT_MYT_HTTP_PORT = int(os.environ.get("MYT_HTTP_PORT", "9082"))
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MYT_HTTP_TIMEOUT", "10"))


def adb_target_host(adb_target: str) -> str:
    if ":" not in adb_target:
        return adb_target
    return adb_target.rsplit(":", 1)[0]


def parse_myt_http_endpoint(endpoint: str) -> tuple[str, int]:
    normalized = endpoint.strip()
    host, separator, port_text = normalized.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError(f"invalid MYT HTTP endpoint: {endpoint!r}; expected host:port")
    return host, int(port_text)


def resolve_myt_http_endpoint(
    *,
    endpoint: str | None = None,
    adb_target: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> tuple[str, int]:
    effective_endpoint = endpoint or DEFAULT_MYT_HTTP_ENDPOINT
    if effective_endpoint:
        return parse_myt_http_endpoint(effective_endpoint)

    resolved_host = host
    if not resolved_host and adb_target:
        resolved_host = adb_target_host(adb_target)
    if not resolved_host:
        resolved_host = DEFAULT_MYT_HTTP_HOST

    resolved_port = DEFAULT_MYT_HTTP_PORT if port is None else port
    return resolved_host, resolved_port


class MytHttpApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class MytHttpResponse:
    status_code: int
    body: dict[str, Any]


class MytHttpApi:
    def __init__(
        self,
        host: str = DEFAULT_MYT_HTTP_HOST,
        port: int = DEFAULT_MYT_HTTP_PORT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        # Disable workstation proxy settings for direct device access.
        self._opener = build_opener(ProxyHandler({}))

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> MytHttpResponse:
        query = ""
        if params:
            query = "?" + urlencode(params, doseq=True)
        url = f"{self.base_url}{path}{query}"
        with self._opener.open(url, timeout=self.timeout_seconds) as response:
            status_code = getattr(response, "status", response.getcode())
            payload = response.read().decode("utf-8", "replace")
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MytHttpApiError(f"invalid JSON from {url}: {payload!r}") from exc
        return MytHttpResponse(status_code=status_code, body=body)

    def get_clipboard(self) -> str:
        response = self.request_json("/clipboard")
        return str(response.body.get("data", {}).get("text", ""))

    def set_clipboard(self, text: str) -> bool:
        response = self.request_json("/clipboard", {"cmd": 2, "text": text})
        return bool(response.body.get("data", {}).get("status"))

    def list_files(self, path: str) -> list[dict[str, Any]]:
        response = self.request_json("/files", {"list": path})
        files = response.body.get("files", [])
        if not isinstance(files, list):
            raise MytHttpApiError(f"unexpected files payload: {response.body!r}")
        return files

    def modify_device(self, cmd: int, **params: Any) -> dict[str, Any]:
        query = {"cmd": cmd, **params}
        response = self.request_json("/modifydev", query)
        return response.body

    def switch_default_ime(self, ime_id: str) -> dict[str, Any]:
        return self.modify_device(20, imeid=ime_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interact with the MYTOS HTTP Android API.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_MYT_HTTP_ENDPOINT,
        help="MYTOS HTTP endpoint in host:port form.",
    )
    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_MYT_HTTP_PORT, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("clipboard-get")

    clipboard_set = subparsers.add_parser("clipboard-set")
    clipboard_set.add_argument("text")

    files = subparsers.add_parser("files")
    files.add_argument("path")

    ime = subparsers.add_parser("ime-switch")
    ime.add_argument("ime_id")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host, port = resolve_myt_http_endpoint(
        endpoint=args.endpoint,
        host=args.host,
        port=args.port,
    )
    api = MytHttpApi(host=host, port=port)

    if args.command == "clipboard-get":
        print(api.get_clipboard())
        return 0
    if args.command == "clipboard-set":
        print(json.dumps({"ok": api.set_clipboard(args.text)}, ensure_ascii=False))
        return 0
    if args.command == "files":
        print(json.dumps(api.list_files(args.path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "ime-switch":
        print(json.dumps(api.switch_default_ime(args.ime_id), ensure_ascii=False))
        return 0
    raise MytHttpApiError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
