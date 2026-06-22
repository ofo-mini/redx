# Decisions

## 2026-04-18 - 外部回传开关由命令行显式覆盖环境变量
- Context: 写库进程需要在不同运行场景下快速切换是否回传外部服务器，单改 `.env` 不够灵活。
- Decision: 在 `xhs_db_writer.py` 增加 `--send-external` 和 `--no-send-external`，优先级高于 `MT_CALLBACK_ENABLED`。
- Alternatives considered: 仅保留 `.env` 配置；单独增加新脚本。
- Consequences: 同一份部署可以按启动命令切换行为，运维成本更低，但需要通过启动日志确认实际生效状态。

## 2026-05-21 - 小红书登录抓包拆分 HTTP/HTTPS 透明端口
- Context: slot2 中将 app UID 的 TCP 80/443 都 DNAT 到 Burp `192.168.50.3:8090` 时，小红书停在启动页并出现 OkHttp read timeout。
- Decision: `xhs_login.py` 支持 `--proxy-https-port`；当前环境使用 80 -> `192.168.50.3:8090`、443 -> `192.168.50.3:18974`。
- Alternatives considered: 继续仅用 `8090`；禁用透明代理只做登录；使用 Android 全局显式代理。
- Consequences: 启动和登录链路可通过透明抓包路径运行，但需要确认 Burp 端 `18974` 监听保持可用。
