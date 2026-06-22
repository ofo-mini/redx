#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler
from urllib.request import Request
from urllib.request import build_opener


DEFAULT_OCR_URL = os.environ.get("OCR_URL", "http://47.110.55.190:5000/ocr")
DEFAULT_OCR_TOKEN = os.environ.get("OCR_BEARER_TOKEN", "yyb-ocr-20260413")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("OCR_TIMEOUT", "120"))


class OcrClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrItem:
    text: str
    conf: float
    coords: tuple[tuple[int, int], ...]

    @property
    def center(self) -> tuple[int, int]:
        xs = [point[0] for point in self.coords]
        ys = [point[1] for point in self.coords]
        return (sum(xs) // len(xs), sum(ys) // len(ys))


class OcrClient:
    def __init__(
        self,
        url: str = DEFAULT_OCR_URL,
        bearer_token: str = DEFAULT_OCR_TOKEN,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(ProxyHandler({}))

    def recognize_file(self, image_path: str | Path) -> list[OcrItem]:
        if not self.bearer_token:
            raise OcrClientError("OCR bearer token is required")
        path = Path(image_path)
        if not path.is_file():
            raise OcrClientError(f"OCR image not found: {path}")

        body, content_type = self._build_multipart_body(path)
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": content_type,
        }
        request = Request(self.url, data=body, headers=headers, method="POST")
        with self._opener.open(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8", "replace")
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OcrClientError(f"invalid OCR JSON: {payload!r}") from exc

        data = doc.get("data", [])
        if not isinstance(data, list):
            raise OcrClientError(f"unexpected OCR payload: {doc!r}")

        items: list[OcrItem] = []
        for raw_item in data:
            text = str(raw_item.get("text", "")).strip()
            coords_raw = raw_item.get("coords", [])
            if not text or not isinstance(coords_raw, list) or not coords_raw:
                continue
            coords: list[tuple[int, int]] = []
            for point in coords_raw:
                if (
                    isinstance(point, list)
                    and len(point) == 2
                    and all(isinstance(value, (int, float)) for value in point)
                ):
                    coords.append((int(point[0]), int(point[1])))
            if not coords:
                continue
            items.append(
                OcrItem(
                    text=text,
                    conf=float(raw_item.get("conf", 0.0)),
                    coords=tuple(coords),
                )
            )
        return items

    def _build_multipart_body(self, image_path: Path) -> tuple[bytes, str]:
        boundary = f"----CodexOcr{uuid.uuid4().hex}"
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{image_path.name}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        return head + image_path.read_bytes() + tail, f"multipart/form-data; boundary={boundary}"
