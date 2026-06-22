# Session Summary

## 2026-05-21

- 使用 `myt-v3-transparent-proxy` 流程检查 slot2：ADB `192.168.50.146:30100` 可用，MYT Host API `:8000` 和 HTTP API `:30101` 本次未响应。
- 安装并启动 `106_97a42428b20ee3c829dddbd0655a4eed.apk`，包名 `com.xingin.xhs`，启动 Activity `com.xingin.xhs/.index.v2.IndexActivityV2`。
- 修改 `xhs_login.py`：加入 Burp CA 注入、`--proxy-https-port`、通知权限弹窗处理、验证码输入后点击登录、风险账号识别和换号重试。
- 透明代理验证：Burp CA `9a5ba575.0` 已挂到 Android system/Conscrypt CA 目录；当前有效 iptables 为 app UID TCP 80 -> `192.168.50.3:8090`、TCP 443 -> `192.168.50.3:18974`。
- 验证结果：脚本能获取手机号和短信验证码并完成登录提交，但本轮多个账号登录后进入 `RiskInterceptActivity` 的“账号封禁说明”，未获得可用登录态。
- 下次继续：更换可用账号池后运行 `python3 xhs_login.py --adb-target 192.168.50.146:30100 --proxy-host 192.168.50.3 --proxy-port 8090 --proxy-https-port 18974 --max-account-retries 3`。

## 2026-05-21 Explicit Proxy Retest

- 清空 slot2 透明 iptables/DNS DNAT，设置 Android global proxy 为 `192.168.50.3:8090`，运行 `xhs_login.py --skip-network-config`。
- 显式代理下 app 可进入登录页和短信页；脚本已补充 app 内通知确认弹窗、账号封禁/下线复查，避免底部导航背景造成误报。
- 本轮手机号 `13189781050` 等满 24 次未返回短信码，已中断手动输入分支；当前设备仍保持 global proxy `192.168.50.3:8090`，iptables NAT OUTPUT 为默认 `ACCEPT`。
- 用户要求去掉代理后，已删除 Android global proxy；当前 `http_proxy=null`，iptables NAT OUTPUT 仍为默认 `ACCEPT`。

## 2026-05-21 No Proxy Login

- 按无代理方式执行 `xhs_login.py --skip-ca-injection --skip-network-config --max-account-retries 3`。
- 登录成功：手机号 `13044260070`，短信验证码 `156031`；成功截图 `screenshots/010_success.png`，5 秒后复查截图 `screenshots/no_proxy_post_success_check.png`。
- 当前设备保持无代理：Android global proxy 为 `null`，iptables NAT OUTPUT 为默认 `ACCEPT`，前台为 `com.xingin.xhs/.index.v2.IndexActivityV2`。

## 2026-05-22 Transparent Proxy Enabled

- 给 slot2 `192.168.50.146:30100` 重新设置 Burp 透明代理，目标为小红书 app UID `10122`。
- 已注入 Burp CA `9a5ba575.0` 到 Android system/Conscrypt CA 目录。
- 当前规则：TCP 80 -> `192.168.50.3:8090`，TCP 443 -> `192.168.50.3:18974`，DNS UDP 53 -> `192.168.50.1:53`；Android global proxy 仍为 `null`。
