# send_mt 复用说明

本文档只说明 MT 上报能力本身，方便其他项目直接复用。

## 代码位置

- 通用客户端：
  - [mt_client.py](/home/waicai_xxx/app/send_out/mt_client.py)
- 当前 worker 调用入口：
  - [send_out_mt_worker.py](/home/waicai_xxx/app/scripts/send_out_mt_worker.py)

## 提供的能力

`app.send_out.mt_client` 里已经抽出三类可复用能力：

1. `MtClient.upload_eserep(...)`
   作用：调用 MT 的 `/p/j/eserep` 上报接口。
2. `MtClient.upload_mngrep(...)`
   作用：调用 MT 的 `/p/j/mngrep` 上报接口。
3. `MtClient.send_with_title_data(...)`
   作用：给定 `title_data` 后，自动按当前项目规则组装参数并选择 `eserep` 或 `mngrep`。

另外还提供：

- `build_payload_for_key(key, data)`
  作用：把不同 key 的原始抓取结果转换成统一发送上下文。
- `zip_data(data)`
  作用：gzip + base64 压缩。

## 适用的 key

- `2viqbGfbeBVnbBv`
  - 走 `eserep`
  - 平台视为 `ct_no_login`
- `JUXTgk6yLjQF8Cg`
  - 走 `eserep`
  - 平台视为 `qnr_out`
- `9bTjTxSFtsRh6Tlr28P1`
  - 走 `eserep`
  - 平台视为 `zhixing`
- `Gdw1zVAUce89YZLBcPpn`
  - 走 `mngrep`
  - `data` 直接发 JSON，不压缩
- `1IK1RZUJPGcY21xaLQXg`
  - 走 `mngrep`
  - `data` 做 gzip + base64 压缩
- `Zb1mlQkLpBLoe34d9TtX`
  - 走 `noncerep`
  - 平台视为 `xhs`
  - 不依赖 seed/title_data，直接使用固定 `param` 组装

## 最简单的复用方式

如果你的项目已经拿到了和本项目一致的 `title_data`，直接调用 `send_with_title_data(...)`。

```python
from app.send_out.mt_client import MtClient, build_payload_for_key

client = MtClient()

key = "1IK1RZUJPGcY21xaLQXg"
item_id = "trace-id-xxx"
raw_data = [{"hotelId": "123"}]
title_data = {
    "collectionParamMap": {},
    "scheduleInfo": {},
    "traceId": "trace-id-xxx",
}

platform, code, check_in_date, check_out_date, hotel_name, poi_id, new_data = build_payload_for_key(key, raw_data)
result = client.send_with_title_data(
    key=key,
    item_id=item_id,
    platform=platform,
    code=code,
    data=new_data,
    title_data=title_data,
    check_in_date=check_in_date,
    check_out_date=check_out_date,
    hotel_name=hotel_name,
    poi_id=poi_id,
)
print(result)
```

返回结构示例：

```python
{
    "mode": "mngrep",
    "ok": True,
    "response": {"code": 200, "message": "操作成功"},
    "final_code": 20,
}
```

## 只想直接调接口

### 1. `eserep`

```python
from app.send_out.mt_client import MtClient, zip_data
import json

client = MtClient()

payload = {"a": 1}
compressed = zip_data(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
param = {
    "source": "3",
    "__traceId": "trace-id-xxx",
    "checkedParam": {
        "checkOutDate": "2026-04-17",
        "poiId": "123",
        "checkInDate": "2026-04-16",
        "poiName": "hotel",
        "collectionTime": 1770000000000,
    },
}

res = client.upload_eserep(
    code=20,
    param=param,
    ct_data=compressed,
    trace_id="trace-id-xxx",
    key="2viqbGfbeBVnbBv",
)
print(res)
```

### 2. `mngrep`

```python
from app.send_out.mt_client import MtClient

client = MtClient()

title_data = {
    "collectionParamMap": {
        "originCheckInDate": "2026-04-16",
    },
    "scheduleInfo": {
        "taskId": 1,
    },
    "traceId": "trace-id-xxx",
}

biz_data = [{"hotelId": "123"}]

res = client.upload_mngrep(
    code=20,
    biz_data=biz_data,
    title_data=title_data,
    trace_id="trace-id-xxx",
    key="Gdw1zVAUce89YZLBcPpn",
)
print(res)
```

## 对接时必须准备的数据

`send_with_title_data(...)` 依赖以下字段：

- `title_data.collectionParamMap`
- `title_data.scheduleInfo`
- `title_data.traceId`

如果你直接使用 `upload_eserep(...)` 或 `upload_mngrep(...)`，则需要你自己提前把请求体组装好。

## 现有 worker 为什么还能正常工作

当前 worker 只是多做了三件事：

1. 从 Redis 取 `title_data`
2. 成功后写 `send_to_mt_ids_xxx`
3. 成功后累加 `send_to_mt_success_num_{collect_channel}`

真正的 MT 协议签名、压缩、请求体拼装、分 key 路由，已经转移到 `mt_client.py`。
