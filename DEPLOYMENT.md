# Deployment

## 服务器路径

- 代码目录：`/opt/xhs-fetcher`
- 服务器：`root@47.103.84.150`

## 安装依赖

```bash
cd /opt/xhs-fetcher
python3 -m pip install --break-system-packages -r requirements.txt
```

这次切到 MongoDB 后，新增依赖：

- `pymongo`

## 必需环境变量

写在 `/opt/xhs-fetcher/.env`：

```env
DB_HOST=qq.rwlb.rds.aliyuncs.com
DB_USER=data
DB_PASSWORD=AbHGL8jMwMPmzM
DB_NAME=data
DB_PORT=3306

REDIS_HOST=...
REDIS_PASSWORD=...

REDIS_HOST_RESULT=...

MONGODB_HOST=...
MONGODB_DB=data
MONGODB_COLLECTION=xhs_initial_state_capture

XHS_COOKIE=...
```

可选 MT 回传配置：

```env
MT_CALLBACK_ENABLED=1
MT_CALLBACK_KEY=2viqbGfbeBVnbBv
MT_CALLBACK_TIMEOUT=30
```

## 启动命令

抓取端（MySQL 种子）：

```bash
cd /opt/xhs-fetcher
python3 -u xhs_fetch_worker.py --concurrency 200 --cookie "$XHS_COOKIE"
```

抓取端（MongoDB `xhs_url` 直拉任务）：

```bash
cd /opt/xhs-fetcher
python3 -u xhs_fetch_worker_mongo.py --concurrency 200 --cookie "$XHS_COOKIE"
```

这个版本不经过任务 Redis，而是直接从 MongoDB `xhs_url` 集合拉取任务；运行游标保存在同库的 `xhs_runtime_state` 集合。抓取结果仍写入结果 Redis。

如果 Mongo 源集合不在默认库名或集合名，可额外传：

```bash
python3 -u xhs_fetch_worker_mongo.py --source-db data --source-collection xhs_url --concurrency 200 --cookie "$XHS_COOKIE"
```

默认日志只打印失败明细和批次汇总。需要周期性成功日志时，可额外传：

```bash
--log-success-every 1000
```

写库端：

```bash
cd /opt/xhs-fetcher
python3 -u xhs_db_writer.py --db-batch-size 300 --idle-sleep 3
```

## 升级步骤

1. 同步代码到 `/opt/xhs-fetcher`
2. 安装最新 `requirements.txt`
3. 确认 `.env` 里已有 `MONGODB_HOST`
4. 如需回传 MT，确认 `.env` 里 `MT_CALLBACK_ENABLED` 配置符合预期，或直接用 `--send-external/--no-send-external` 覆盖
5. 重启抓取端和写库端

## 验证

抓取端应看到：

- `startup_begin`
- `startup`
- `batch_rows`
- `row_done`
- `batch_done`

写库端应看到：

- `startup`
- `batch_rows`
- `batch_done`
- `idle_wait`

如果 MT 回传启用，写库日志里还会看到：

- `mt_callback_enabled`
- `mt_callback_key`
- `batch_mt_callback_success`
- `batch_mt_callback_failed`
- `mt_callback_trace`

## 当前行为

- 源表仍然支持 MySQL `xhs_url`
- MongoDB 也可以作为直连任务源，使用 `xhs_fetch_worker_mongo.py` 直接从 `xhs_url` 集合抓取
- 结果不再写 MySQL
- 结果主存储是 MongoDB
- MongoDB 成功写入后会再回传 MT，且优先只回传 `$.note.noteDetailMap.<note_id>` 到 `data`
- 标题为 `小红书 - 你访问的页面不见了` 的记录不会回传 MT
- MongoDB 会额外保存一个独立字段 `lastUpdateTime`
- 任务 Redis 和结果 Redis 分离
- 抓取前会自动清洗源 URL，只请求 `xiaohongshu.com` 域名链接
- 请求异常时默认自动换代理重试，单条任务默认最多 5 次
- 中间重试失败不记入 `failed` 统计，只记录最终失败
