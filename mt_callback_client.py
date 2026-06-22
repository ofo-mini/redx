#!/usr/bin/env python3
import base64
import gzip
import hashlib
import io
import json
import random
import string
import time
from dataclasses import dataclass

import requests


DEFAULT_ESEREP_MT_KEY = "2viqbGfbeBVnbBv"
DEFAULT_FUNCTION_NAME = "Wijsekv2"


@dataclass(frozen=True)
class MtEndpoint:
    host: str
    path: str
    app_key: str
    app_secret: str


ESEREP_ENDPOINT = MtEndpoint(
    host="www.mxc2w35pzy.com",
    path="/p/j/eserep",
    app_key="V2U4W",
    app_secret="PBPDGVHZITZ4BSNNAOVI6YWQQGB8D614",
)


def zip_data(data: str) -> str:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f_out:
        f_out.write(data.encode("utf-8"))
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def calc_md5(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def get_nonce() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=10))


def get_numeric_trace_id() -> str:
    return "".join(random.choices(string.digits, k=27))


def build_signed_url(endpoint: MtEndpoint) -> str:
    ts = int(time.time() * 1000)
    nonce = get_nonce()
    sign = calc_md5(
        f"https://{endpoint.host}{endpoint.path}&{endpoint.app_key}&{endpoint.app_secret}&{ts}&{nonce}"
    )
    return (
        f"https://{endpoint.host}{endpoint.path}"
        f"?appkey={endpoint.app_key}&ts={ts}&nonce={nonce}&sign={sign}"
    )


class MtClient:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout

    def upload_eserep(self, code: int, param: dict, data_payload, trace_id: str, key: str = "") -> dict:
        data = {
            "code": code,
            "message": "抓取成功",
            "data": data_payload,
            "param": param,
            "extendData": "",
        }
        res = requests.post(build_signed_url(ESEREP_ENDPOINT), json=data, timeout=self.timeout)
        return res.json()

    def send_via_eserep(self, *, key: str, item_id: str, data: dict) -> dict:
        trace_id = get_numeric_trace_id()
        param = {
            "__source": 2,
            "__companyId": 1036,
            "__crawlerType": 2,
            "__env": "prod",
            "__taskId": 2707,
            "__groupId": 1699708,
            "__businessId": 10017,
            "__functionId": 1944,
            "__functionName": DEFAULT_FUNCTION_NAME,
            "__traceId": trace_id,
        }
        request_url = build_signed_url(ESEREP_ENDPOINT)
        response = requests.post(request_url, json={
            "code": 20,
            "message": "抓取成功",
            "data": data,
            "param": param,
            "extendData": "",
        }, timeout=self.timeout).json()
        return {
            "mode": "eserep",
            "ok": response.get("code") == 200,
            "response": response,
            "final_code": 20,
            "trace_id": trace_id,
            "request_url": request_url,
            "request_host": ESEREP_ENDPOINT.host,
            "request_path": ESEREP_ENDPOINT.path,
            "param": param,
        }
