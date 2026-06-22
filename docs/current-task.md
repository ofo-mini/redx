# Current Task
 
 ## Goal
 
 实现小红书模拟操作登录脚本，能连接 ADB 自动完成卸载、重装、打开 app，配合 OCR，并动态配置 iptables 容器转发流量到 Burp (192.168.50.3:8090) 并自动接码/交互登录。
 
## Current State

- 已将动态 UID 解析、iptables 清理重构、DNS UDP 转发功能整合入 `xhs_login.py`。
- 接码与手机号获取 API 逻辑已编写并集成，并优化了登录状态检测机制以解决页面切换时的误判。
- 2026-05-21：slot2 ADB `192.168.50.146:30100` 可连接；APK `106_97a42428b20ee3c829dddbd0655a4eed.apk` 已安装并能启动 `com.xingin.xhs/.index.v2.IndexActivityV2`。
- 2026-05-21：`xhs_login.py` 已加入 Burp CA 注入、可配置透明代理参数、Android 通知权限处理、验证码登录按钮点击、风险账号检测和换号重试。
- 2026-05-21：验证 `192.168.50.3:8090` 同时承载 80/443 会导致 app 启动页请求超时；当前可用组合为 TCP 80 DNAT 到 `192.168.50.3:8090`，TCP 443 DNAT 到 `192.168.50.3:18974`。
- 2026-05-21：按显式代理模式测试：已清空透明 iptables，设置 Android global proxy 为 `192.168.50.3:8090`，用 `--skip-network-config` 可进入登录和短信页。
- 2026-05-21：`xhs_login.py` 已补充 app 内“打开通知/确认”弹窗处理，以及成功判定前账号下线/封禁二次复查。
- 2026-05-21：用户要求去掉代理后，slot2 已清空 Android global proxy，且透明 iptables 规则仍为空。
- 2026-05-21：无代理模式重跑登录成功，账号 `13044260070` 短信验证码 `156031`，当前停在小红书首页。
- 2026-05-22：已按用户要求重新给 slot2 设置 Burp 透明代理：小红书 UID `10122` 的 TCP 80 -> `192.168.50.3:8090`，TCP 443 -> `192.168.50.3:18974`，DNS UDP 53 -> `192.168.50.1:53`，Android global proxy 仍为 `null`。
 
## Next Step

- 当前 slot2 已开启小红书 app UID 级 Burp 透明代理；如需重新登录且不走代理，需先清理 iptables 后执行：`python3 xhs_login.py --adb-target 192.168.50.146:30100 --skip-ca-injection --skip-network-config --max-account-retries 3`。
 
## Blockers

- 无。
 
## Risks

- 如果接码平台无对应业务的手机号可用、验证码拉取超时或账号被风险拦截，脚本会失败或换号重试；重试会消耗账号池号码。
 
## Last Verified

- 2026-05-21：完整运行安装、清数据、CA 注入、iptables、OCR 登录、手机号获取、短信验证码获取与输入；3 个换号尝试均在登录后进入风险拦截页。
- 2026-05-21：当前 slot2 仍保留 Burp CA `9a5ba575.0`，iptables 规则为 app UID TCP 80 -> `192.168.50.3:8090`、TCP 443 -> `192.168.50.3:18974`，Android global http_proxy 为 `null`。
- 2026-05-21：当前 Android global http_proxy 为 `null`，iptables NAT OUTPUT 只有默认 `ACCEPT`，当前停在 `com.xingin.login.activity.LoginActivity` 短信页。
- 2026-05-21：无代理登录成功后等待 5 秒复查，前台为 `com.xingin.xhs/.index.v2.IndexActivityV2`，OCR 未见账号封禁/下线提示。
- 2026-05-22：验证 slot2 ADB `device`、Burp CA `9a5ba575.0` 已在 system/Conscrypt CA 目录、iptables OUTPUT 已跳转到 `CODEX_MITM_7b31487b07ecebeb`。
