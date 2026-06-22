# XHS Capture Pipeline

当前主链路已经切成两段：

- `xhs_fetch_worker.py`
  从 MySQL `xhs_url` 读取种子，抓取小红书页面，解析 `window.__INITIAL_STATE__`，把结果写入结果 Redis。
- `xhs_db_writer.py`
  从结果 Redis 消费结果，批量写入 MongoDB，并在写入成功后按 MT `eserep` 协议回传。

源数据仍在 MySQL，抓取结果主存储已经切到 MongoDB。

## 依赖

```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

## 配置

脚本会自动读取 `.env`。

必需配置：

- `DB_HOST`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `DB_PORT`
- `REDIS_HOST`
- `REDIS_PASSWORD`
- `REDIS_HOST_RESULT`
- `MONGODB_HOST`

可选配置：

- `MONGODB_DB`
- `MONGODB_COLLECTION`
- `REDIS_PORT`
- `REDIS_DB`
- `REDIS_PORT_RESULT`
- `REDIS_DB_RESULT`
- `XHS_COOKIE`
- `MT_CALLBACK_ENABLED`
- `MT_CALLBACK_KEY`
- `MT_CALLBACK_TIMEOUT`

## 运行

抓取端（MySQL 种子）：

```bash
python3 -u xhs_fetch_worker.py --concurrency 200 --cookie "$XHS_COOKIE"
```

抓取端（MongoDB `xhs_url` 直拉任务）：

```bash
python3 -u xhs_fetch_worker_mongo.py --concurrency 200 --cookie "$XHS_COOKIE"
```

这个版本不使用任务 Redis，而是直接从 MongoDB `xhs_url` 集合按游标批量拉取并发抓取；抓取结果仍进入结果 Redis，供 `xhs_db_writer.py` 消费。默认运行游标保存在同库集合 `xhs_runtime_state`。

默认源集合是当前 `MONGODB_DB` 下的 `xhs_url`；也可以显式指定：

```bash
python3 -u xhs_fetch_worker_mongo.py --source-db data --source-collection xhs_url --concurrency 200 --cookie "$XHS_COOKIE"
```

默认只打印失败明细和批次汇总；如果需要定期打印成功明细，可以加：

```bash
python3 -u xhs_fetch_worker.py --concurrency 200 --log-success-every 1000 --cookie "$XHS_COOKIE"
```

写库端：

```bash
python3 -u xhs_db_writer.py --db-batch-size 300 --idle-sleep 3
```

默认 MT 回传并发数等于 `--db-batch-size`。如果要单独指定，可以加：

```bash
python3 -u xhs_db_writer.py --db-batch-size 300 --mt-callback-workers 300
```

是否发送到外部服务器可由命令行直接覆盖：

```bash
python3 -u xhs_db_writer.py --db-batch-size 300 --no-send-external
python3 -u xhs_db_writer.py --db-batch-size 300 --send-external
```

## 数据流

1. MySQL `xhs_url` 提供源 URL。
2. 抓取端把待处理任务种入任务 Redis。
3. 抓取端消费任务 Redis，抓页面并解析出 `initial_state_json`，并保留为原生 JSON 对象。
4. 写入 MongoDB 时会优先按 `note.noteDetailMap.<note_id>.note.lastUpdateTime` 提取 `lastUpdateTime`，取不到再递归回退。
5. 成功结果和最终失败结果进入结果 Redis。
6. 写库端从结果 Redis 批量写入 MongoDB 集合 `xhs_initial_state_capture`。
7. MongoDB 写入成功后，写库端按 MT `eserep` 协议回传一次，回传体只发送 `$.note.noteDetailMap.<note_id>` 对应的对象，字段名仍然是 `data`；取不到时回退到整个 `initial_state_json`。

## 存储内容

MongoDB 当前只保留：

- `source_id`
- `note_id`
- `url`
- `http_status`
- `final_url`
- `title`
- `description`
- `keywords`
- `proxy_url`
- `proxy_out_ip`
- `proxy_fetch_id`
- `retry_count`
- `initial_state_json`
- `lastUpdateTime`
- `parsed_summary`
- `parse_error`
- `captured_at`

不再保存：

- 整页 HTML
- `initial_state_raw`

## 说明

- 可重试抓取失败会直接回抓取队列，不写 MongoDB。
- 请求阶段失败默认会自动换代理重试，单条任务默认最多 5 次；明显的永久错误不会重试。
- 中间重试失败不会打印逐条失败日志，也不会计入 `failed`；`failed` 只统计最终失败。
- 续传判断已经改成读取 MongoDB 里的历史结果，不再依赖 MySQL 结果表。
- 结果 Redis 和任务 Redis 可以分开部署。
- 源表里的脏 URL 会在请求前自动清洗；如果一条记录里混有多个链接，只保留 `xiaohongshu.com` 域名的链接。
- MT 回传默认开启，默认 key 是 `2viqbGfbeBVnbBv`，使用当前项目内置的 `eserep` 客户端实现，`param` 使用固定值，且所有固定字段都带双下划线前缀；默认 `__functionName=Wijsekv2`，并额外带 27 位纯数字 `__traceId`。
- 如果标题是 `小红书 - 你访问的页面不见了`，结果既不写 Mongo，也不回传 MT。
- MT 回传只发送 `lastUpdateTime` 在近 7 天内的帖子；超时或错误默认重试 3 次。
