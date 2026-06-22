import base64
import datetime
import gzip
import hashlib
import io
import json
import logging
import random
import string
import time
from dataclasses import dataclass

import requests


QNR_OUT_MT_KEYS = {"JUXTgk6yLjQF8Cg"}
ZHIXING_MT_KEYS = {"9bTjTxSFtsRh6Tlr28P1", "Gdw1zVAUce89YZLBcPpn", "1IK1RZUJPGcY21xaLQXg"}
SPECIAL_PAYLOAD_KEYS = {"Gdw1zVAUce89YZLBcPpn", "1IK1RZUJPGcY21xaLQXg"}
MNGREP_MT_KEYS = {"Gdw1zVAUce89YZLBcPpn", "1IK1RZUJPGcY21xaLQXg"}
COMPRESSED_MNGREP_KEYS = {"1IK1RZUJPGcY21xaLQXg"}
NONCEREP_MT_KEYS = {"Zb1mlQkLpBLoe34d9TtX"}


@dataclass(frozen=True)
class MtEndpoint:
    host: str
    path: str
    app_key: str
    app_secret: str


ESEREP_ENDPOINT = MtEndpoint(
    host="www.jncm1mncbq.com",
    path="/p/j/eserep",
    app_key="YGIJ9",
    app_secret="EOWQWUQMXPDG4XIDNRRQEEI0LNVPGWNO",
)

MNGREP_ENDPOINT = MtEndpoint(
    host="www.co4abgoxvv.com",
    path="/p/j/mngrep",
    app_key="YGIJ9",
    app_secret="EOWQWUQMXPDG4XIDNRRQEEI0LNVPGWNO",
)

NONCEREP_ENDPOINT = MtEndpoint(
    host="www.co4abgoxvv.com",
    path="/p/j/noncerep",
    app_key="YGIJ9",
    app_secret="EOWQWUQMXPDG4XIDNRRQEEI0LNVPGWNO",
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


def save_qnr_out_data(ct_data: dict) -> tuple[int, str, str, str, str, str]:
    code = ct_data.get("nnnn_code", 20)
    title = ((ct_data.get("data") or {}).get("noPriceTip") or {}).get("title")
    if title:
        if "酒店火爆，该日期所有房型已满" in title:
            code = 1010
        if "临时停业" in title:
            code = 1008
    return code, "", "", "", "", ""


def save_zhixing_data(ct_data: dict) -> tuple[int, str, str, str, str, str]:
    poi_id = ct_data.get("hotelId", "")
    return 20, "", "", "", poi_id, ""


def build_payload_for_key(key: str, data: dict | list | str) -> tuple[str, int, str, str, str, str, dict | list | str]:
    if key == "2viqbGfbeBVnbBv":
        platform = "ct_no_login"
        code = data.get("code")
        new_data = data.get("data")
        if not isinstance(new_data, dict):
            new_data = {"original_data": data}
        if code != 1009:
            no_room_title = ((data.get("data") or {}).get("noRoomTip") or {}).get("title", "")
            if "歇业" in no_room_title:
                code = 1008
            elif "暂无可预订房型" in no_room_title:
                code = 1007
            else:
                code = 20
        return platform, code, "", "", "", "", new_data

    if key in QNR_OUT_MT_KEYS:
        platform = "qnr_out"
        code, check_in_date, check_out_date, hotel_name, poi_id, get_time = save_qnr_out_data(data)
        return platform, code, check_in_date, check_out_date, hotel_name, poi_id, data

    if key in MNGREP_MT_KEYS:
        return "zhixing", 20, "", "", "", "", data

    if key in ZHIXING_MT_KEYS:
        platform = "zhixing"
        code, check_in_date, check_out_date, hotel_name, poi_id, get_time = save_zhixing_data(data)
        return platform, code, check_in_date, check_out_date, hotel_name, poi_id, data

    if key in NONCEREP_MT_KEYS:
        return "xhs", 20, "", "", "", "", data

    raise ValueError(f"unsupported key {key}")


class MtClient:
    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        special_payload_logger: logging.Logger | None = None,
        timeout: int = 300,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.special_payload_logger = special_payload_logger
        self.timeout = timeout

    def log_special_payload(self, stage: str, key: str, item_id: str, payload) -> None:
        if key not in SPECIAL_PAYLOAD_KEYS or self.special_payload_logger is None:
            return
        try:
            message = json.dumps(
                {
                    "stage": stage,
                    "key": key,
                    "item_id": item_id,
                    "payload": payload,
                },
                ensure_ascii=False,
            )
        except Exception:
            message = f'{{"stage":"{stage}","key":"{key}","item_id":"{item_id}","payload_repr":"{repr(payload)}"}}'
        self.special_payload_logger.info(message)

    def upload_eserep(self, code: int, param: dict, ct_data: str, trace_id: str, key: str = "") -> dict:
        data = {
            "code": code,
            "message": "抓取成功",
            "data": ct_data,
            "param": param,
            "extendData": "",
        }
        self.log_special_payload("mt_request", key, trace_id, data)
        res = requests.post(build_signed_url(ESEREP_ENDPOINT), json=data, timeout=self.timeout)
        self.logger.info("%s upload response time ms=%.2f", trace_id, res.elapsed.total_seconds() * 1000)
        response_json = res.json()
        self.log_special_payload("mt_response", key, trace_id, response_json)
        return response_json

    def upload_mngrep(self, code: int, biz_data, title_data: dict, trace_id: str, key: str = "") -> dict:
        collection_param_map = dict(title_data.get("collectionParamMap") or {})
        schedule_info = dict(title_data.get("scheduleInfo") or {})
        task_trace_id = title_data.get("traceId") or trace_id
        compress_payload = key in COMPRESSED_MNGREP_KEYS
        if compress_payload:
            collection_param_map["__compressTag"] = "1"
        else:
            collection_param_map.pop("__compressTag", None)

        checked_param = {
            "collectionTime": str(int(time.time() * 1000)),
            "checkInDate": collection_param_map.get("originCheckInDate", ""),
            "checkOutDate": "",
            "poiId": "",
            "poiName": "",
        }
        checkin = collection_param_map.get("originCheckInDate")
        if checkin:
            try:
                checkout = datetime.datetime.strptime(checkin, "%Y-%m-%d") + datetime.timedelta(days=1)
                checked_param["checkOutDate"] = checkout.strftime("%Y-%m-%d")
            except Exception:
                pass

        param = {
            "collectionParamMap": collection_param_map,
            "scheduleInfo": schedule_info,
            "traceId": task_trace_id,
            "checkedParam": checked_param,
        }

        if compress_payload:
            middle_biz_data = zip_data(json.dumps(biz_data, ensure_ascii=False, separators=(",", ":")))
        else:
            middle_biz_data = biz_data

        middle_data = {
            "data": middle_biz_data,
            "param": param,
        }
        request_body = {
            "code": code,
            "message": "抓取成功",
            "data": json.dumps(middle_data, ensure_ascii=False, separators=(",", ":")),
        }
        self.log_special_payload("mt_request", key, trace_id, request_body)
        json_data = json.dumps(request_body, ensure_ascii=False).replace("\u001a", "").replace("\u001A", "")
        headers = {"Content-Type": "application/json;charset=utf-8"}
        res = requests.post(
            build_signed_url(MNGREP_ENDPOINT),
            data=json_data.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        self.logger.info("%s upload response time ms=%.2f", trace_id, res.elapsed.total_seconds() * 1000)
        response_json = res.json()
        self.log_special_payload("mt_response", key, trace_id, response_json)
        return response_json

    def upload_noncerep(self, code: int, param: dict, ct_data: str, trace_id: str, key: str = "") -> dict:
        data = {
            "code": code,
            "message": "抓取成功",
            "data": ct_data,
            "param": param,
            "extendData": "",
        }
        self.log_special_payload("mt_request", key, trace_id, data)
        res = requests.post(build_signed_url(NONCEREP_ENDPOINT), json=data, timeout=self.timeout)
        self.logger.info("%s upload response time ms=%.2f", trace_id, res.elapsed.total_seconds() * 1000)
        response_json = res.json()
        self.log_special_payload("mt_response", key, trace_id, response_json)
        return response_json

    def send_with_title_data(
        self,
        *,
        key: str,
        item_id: str,
        platform: str,
        code: int,
        data: dict,
        title_data: dict,
        origin_code: int | None = None,
        check_in_date: str = "",
        check_out_date: str = "",
        hotel_name: str = "",
        poi_id: str = "",
        get_time: str | int = "",
    ) -> dict:
        if key in NONCEREP_MT_KEYS:
            param = {
                "__env": "prod",
                "__traceId": item_id or get_nonce(),
                "__jobId": 1238,
                "__crawlerType": 2,
                "__taskId": 1109,
                "__taskKey": "",
                "__groupId": 636269,
                "__sequenceId": 40018,
                "__businessId": 10017,
                "__functionId": 785,
                "__functionName": "WemeXHSV3",
                "__companyId": 1036,
                "__source": 2,
                "__compressTag": "1",
            }
            zip_ct_data = zip_data(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            response = self.upload_noncerep(20, param, zip_ct_data, item_id, key)
            return {
                "mode": "noncerep",
                "ok": response.get("code") == 200,
                "response": response,
                "final_code": 20,
            }

        if key in MNGREP_MT_KEYS:
            response = self.upload_mngrep(code, data, title_data, item_id, key)
            return {
                "mode": "mngrep",
                "ok": response.get("code") == 200,
                "response": response,
                "final_code": code,
            }

        if key == "2viqbGfbeBVnbBv":
            collection_param_map = title_data.get("collectionParamMap") or {}
            comp_poi_id = collection_param_map.get("poiId")
            check_in = collection_param_map.get("checkInDate")
            timestamp_s = int(check_in) / 1000
            check_in_date_dt = datetime.datetime.fromtimestamp(timestamp_s)
            check_out_date_dt = check_in_date_dt + datetime.timedelta(days=1)
            checkin = check_in_date_dt.strftime("%Y-%m-%d")
            checkout = check_out_date_dt.strftime("%Y-%m-%d")
            comp_poi_name = collection_param_map.get("compPoiName")
            check_in_date = checkin
            check_out_date = checkout
        else:
            collection_param_map = title_data.get("collectionParamMap") or {}
            checkin = collection_param_map.get("originCheckInDate")
            check_out_new = datetime.datetime.strptime(checkin, "%Y-%m-%d") + datetime.timedelta(days=1)
            checkout = check_out_new.strftime("%Y-%m-%d")
            comp_poi_name = collection_param_map.get("compPoiName")
            comp_poi_id = collection_param_map.get("compPoiId")

        if check_in_date == "":
            check_in_date = checkin
            check_out_date = checkout
        if hotel_name == "":
            hotel_name = comp_poi_name
        if poi_id == "":
            poi_id = comp_poi_id
        if get_time == "":
            get_time = int(time.time() * 1000)

        schedule_info = title_data.get("scheduleInfo") or {}
        trace_id = title_data.get("traceId")
        param = {
            "source": collection_param_map.get("source"),
            "__seedType": collection_param_map.get("__seedType"),
            "companyId": collection_param_map.get("companyId"),
            "__crawlerType": title_data.get("crawlerType"),
            "__compressTag": "1",
            "__env": "prod",
            "__traceId": trace_id,
            "__jobId": schedule_info.get("jobId"),
            "__taskId": schedule_info.get("taskId"),
            "__taskKey": schedule_info.get("taskKey"),
            "__groupId": schedule_info.get("groupId"),
            "__sequenceId": schedule_info.get("sequenceId"),
            "__businessId": schedule_info.get("businessId"),
            "__functionId": schedule_info.get("functionId"),
            "__functionName": schedule_info.get("functionName"),
            "__companyId": schedule_info.get("companyId"),
            "__source": schedule_info.get("source"),
            "magic_origin_seed": collection_param_map.get("magic_origin_seed"),
            "checkedParam": {
                "checkOutDate": check_out_date,
                "poiId": poi_id,
                "checkInDate": check_in_date,
                "poiName": hotel_name,
                "collectionTime": get_time,
            },
        }

        if origin_code is None:
            origin_code = code

        data["new_param"] = param
        data["new_code"] = code
        data["origin_code"] = origin_code
        data["key"] = key

        if checkin != check_in_date or str(comp_poi_id) != str(poi_id) or str(code) in {"403", "402", "406", "405"}:
            return {"mode": "eserep", "ok": False, "response": None, "final_code": code, "skipped": "mismatch_or_filtered"}

        if str(code) == "404":
            return {"mode": "eserep", "ok": False, "response": None, "final_code": code, "needs_first_retry_gate": True}

        if item_id[:5] == "inner":
            return {"mode": "eserep", "ok": False, "response": None, "final_code": code, "skipped": "inner_trace_id"}

        zip_ct_data = zip_data(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        response = self.upload_eserep(code, param, zip_ct_data, item_id, key)
        return {
            "mode": "eserep",
            "ok": response.get("code") == 200,
            "response": response,
            "final_code": code,
        }
