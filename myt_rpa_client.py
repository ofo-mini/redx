#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import types
from pathlib import Path


DEFAULT_MYT_RPA_ENDPOINT = os.environ.get("MYT_RPA_ENDPOINT")
DEFAULT_MYT_RPA_HOST = os.environ.get("MYT_RPA_HOST", "192.168.50.148")
DEFAULT_MYT_RPA_PORT = int(os.environ.get("MYT_RPA_PORT", "9083"))
DEFAULT_CONNECT_TIMEOUT = int(os.environ.get("MYT_RPA_CONNECT_TIMEOUT", "10"))


def adb_target_host(adb_target: str) -> str:
    if ":" not in adb_target:
        return adb_target
    return adb_target.rsplit(":", 1)[0]


def parse_myt_rpa_endpoint(endpoint: str) -> tuple[str, int]:
    normalized = endpoint.strip()
    host, separator, port_text = normalized.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        raise ValueError(f"invalid MYT RPA endpoint: {endpoint!r}; expected host:port")
    return host, int(port_text)


def resolve_myt_rpa_endpoint(
    *,
    endpoint: str | None = None,
    adb_target: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> tuple[str, int]:
    effective_endpoint = endpoint or DEFAULT_MYT_RPA_ENDPOINT
    if effective_endpoint:
        return parse_myt_rpa_endpoint(effective_endpoint)

    resolved_host = host
    if not resolved_host and adb_target:
        resolved_host = adb_target_host(adb_target)
    if not resolved_host:
        resolved_host = DEFAULT_MYT_RPA_HOST

    resolved_port = DEFAULT_MYT_RPA_PORT if port is None else port
    return resolved_host, resolved_port


class MytRpaClient:
    def __init__(
        self,
        host: str = DEFAULT_MYT_RPA_HOST,
        port: int = DEFAULT_MYT_RPA_PORT,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._api = None

    def _sdk_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "MYT_RPA_SDK_v10_1_20251009" / "demo_py_x64"

    def connect(self) -> bool:
        if self._api is not None:
            return True
        sdk_dir = self._sdk_dir()
        previous_cwd = Path.cwd()
        if str(sdk_dir) not in sys.path:
            sys.path.insert(0, str(sdk_dir))
        try:
            os.chdir(sdk_dir)
            sys.argv = [str(sdk_dir / "myt_action.py"), "task"]
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
            sys.modules.setdefault("requests", types.ModuleType("requests"))
            from common.mytRpc import MytRpc

            api = MytRpc()
            if not api.init(self.host, self.port, self.connect_timeout):
                return False
            self._api = api
            return True
        finally:
            os.chdir(previous_cwd)

    def send_text(self, text: str) -> bool:
        if not self.connect():
            return False
        return bool(self._api.sendText(text))

    def press_enter(self) -> bool:
        if not self.connect():
            return False
        return bool(self._api.pressEnter())

    def long_click(self, x: int, y: int, seconds: float) -> bool:
        if not self.connect():
            return False
        return bool(self._api.longClick(0, x, y, seconds))
