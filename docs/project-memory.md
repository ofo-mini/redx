# Project Memory

## Goal

维护一套小红书帖子抓取链路：从 MySQL `xhs_url` 读取 URL，经代理抓取页面并解析 `window.__INITIAL_STATE__`，结果写入 MongoDB，并可选回传到外部 MT `eserep` 接口。

## Architecture

- `xhs_fetch_worker.py`：从 MySQL 取种子，清洗 URL，仅保留 `xiaohongshu.com` 域名链接；抓取后将结果写入结果 Redis。
- `xhs_fetch_worker_mongo.py`：直接从 MongoDB `xhs_url` 集合按游标拉取任务抓取，不使用任务 Redis；抓取结果仍进入结果 Redis。
- `xhs_db_writer.py`：从结果 Redis 取解析结果，批量写入 MongoDB；根据开关决定是否回传外部服务器。
- `mt_callback_client.py`：封装 `eserep` 回传逻辑，固定 `param` 字段使用双下划线前缀，并附带 27 位数字 `__traceId`。

## Environments

- 源 MySQL：`qq.rwlb.rds.aliyuncs.com:3306`
- 部署服务器：`root@47.103.84.150`
- 服务器代码目录：`/opt/xhs-fetcher`
- 调试安卓容器 (Slot 2)：`192.168.50.146:30100`
- 抓包代理 (Burp Suite)：`192.168.50.3:8090` 提供 Burp/CA 页面和 HTTP 代理；本机当前可用 HTTPS 透明监听为 `192.168.50.3:18974`。
- 容器 DNS 转发：UDP 53 流量转发到网关 `192.168.50.1` 解决域名解析问题
- OCR 服务：`http://47.110.55.190:5000/ocr` (Authorization: `Bearer yyb-ocr-20260413`)
- 接码/注册 API：
  - 手机号获取: `http://120.26.6.140:56311/v5/account/fetch?business=redx&project_name=r_redx_three_1&account_type=0` (X-API-KEY: `r_redx_three_19ce8e49c4abe4ca3ae0593a5540da21`)
  - 验证码获取: `http://120.26.6.140:56311/v5/account/sms/code?phone_number={phone_number}&business_id=redx`

## Important Paths

- `/home/redx/xhs_fetch_worker.py`
- `/home/redx/xhs_fetch_worker_mongo.py`
- `/home/redx/xhs_db_writer.py`
- `/home/redx/xhs_initial_state_capture.py`
- `/home/redx/mt_callback_client.py`
- `/home/redx/README.md`
- `/home/redx/DEPLOYMENT.md`

## Commands

- 抓取端：`python3 -u xhs_fetch_worker.py --concurrency 200 --cookie "$XHS_COOKIE"`
- 写库端：`python3 -u xhs_db_writer.py --db-batch-size 300 --idle-sleep 3`
- 关闭外部回传：`python3 -u xhs_db_writer.py --db-batch-size 300 --no-send-external`
- 开启外部回传：`python3 -u xhs_db_writer.py --db-batch-size 300 --send-external`
- 模拟登录端：`python3 xhs_login.py --adb-target 192.168.50.146:30100 --proxy-host 192.168.50.3 --proxy-port 8090 --proxy-https-port 18974`

## Constraints

- 默认使用外部代理抓取。
- `initial_state_json` 在 MongoDB 中按原生 JSON 对象保存。
- 标题为 `小红书 - 你访问的页面不见了` 的记录不回传外部服务器，也不写入 MongoDB。
- MT 回传仅发送 `lastUpdateTime` 在近 7 天内的帖子，默认失败重试 3 次。
- 抓取端和写库端默认使用单行刷新进度输出，减少高频日志刷屏。

## Preferences

- 关键状态和操作方法写入项目文档，不依赖聊天记录。
- 运行命令优先给出可直接复制执行的形式。
