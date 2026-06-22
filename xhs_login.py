#!/usr/bin/env python3
import os
import sys
import time
import json
import re
import argparse
import subprocess
import urllib.request
import shlex
from shutil import copyfile
from pathlib import Path
from ocr_client import OcrClient, OcrItem

# Config
DEFAULT_ADB_TARGET = "192.168.50.146:30100"
DEFAULT_APK_PATH = "/Users/one/Documents/home/redx/106_97a42428b20ee3c829dddbd0655a4eed.apk"
DEFAULT_PROXY_HOST = os.environ.get("BURP_TRANSPARENT_HOST", "192.168.50.3")
DEFAULT_PROXY_PORT = int(os.environ.get("BURP_TRANSPARENT_PORT", "8090"))
DEFAULT_PROXY_HTTPS_PORT = int(os.environ.get("BURP_TRANSPARENT_HTTPS_PORT", str(DEFAULT_PROXY_PORT)))
DEFAULT_DNS_HOST = os.environ.get("ANDROID_DNS_HOST", "192.168.50.1")
XHS_PACKAGE = "com.xingin.xhs"
XHS_ACTIVITY = "com.xingin.xhs/.index.v2.IndexActivityV2"
MITM_CHAIN_NAME = "CODEX_MITM_7b31487b07ecebeb"

# Colors for log output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_info(msg):
    print(f"{Colors.OKBLUE}[INFO]{Colors.ENDC} {msg}")

def log_success(msg):
    print(f"{Colors.OKGREEN}[SUCCESS]{Colors.ENDC} {Colors.BOLD}{msg}{Colors.ENDC}")

def log_warning(msg):
    print(f"{Colors.WARNING}[WARNING]{Colors.ENDC} {Colors.BOLD}{msg}{Colors.ENDC}")

def log_error(msg):
    print(f"{Colors.FAIL}[ERROR]{Colors.ENDC} {Colors.BOLD}{msg}{Colors.ENDC}")

def run_cmd(cmd, check=True):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return res.stdout.strip(), res.stderr.strip()
    except subprocess.CalledProcessError as e:
        log_error(f"Command {' '.join(cmd)} failed with exit code {e.returncode}")
        log_error(f"Stdout: {e.stdout}")
        log_error(f"Stderr: {e.stderr}")
        if check:
            raise
        return e.stdout.strip(), e.stderr.strip()

# API Helpers
def fetch_phone_from_api():
    url = "http://120.26.6.140:56311/v5/account/fetch?business=redx&project_name=r_redx_three_1&account_type=0"
    log_info("Fetching phone number from API...")
    req = urllib.request.Request(url)
    req.add_header("X-API-KEY", "r_redx_three_19ce8e49c4abe4ca3ae0593a5540da21")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_str = response.read().decode("utf-8")
            data = json.loads(res_str)
            if data.get("code") == 0 and "data" in data:
                phone = data["data"].get("account_val")
                if phone:
                    log_success(f"Successfully fetched phone number: {phone}")
                    return phone
            log_error(f"Fetch phone API response error: {res_str}")
    except Exception as e:
        log_error(f"Fetch phone API request failed: {e}")
    return None

def fetch_sms_code_from_api(phone):
    url = f"http://120.26.6.140:56311/v5/account/sms/code?phone_number={phone}&business_id=redx"
    log_info(f"Polling SMS code from API for {phone}...")
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_str = response.read().decode("utf-8")
            data = json.loads(res_str)
            if data.get("code") == 0:
                code_data = data.get("data") or data.get("message")
                if code_data:
                    # Find a 6-digit number in the response data
                    digits = re.findall(r"\b\d{6}\b", str(code_data))
                    if digits:
                        return digits[0]
                    if len(str(code_data).strip()) == 6 and str(code_data).strip().isdigit():
                        return str(code_data).strip()
            # code -1 or similar is expected when waiting
    except Exception as e:
        log_error(f"Fetch SMS API request failed: {e}")
    return None

class XhsLoginRunner:
    def __init__(
        self,
        adb_target,
        apk_path,
        phone=None,
        no_install=False,
        proxy_host=DEFAULT_PROXY_HOST,
        proxy_port=DEFAULT_PROXY_PORT,
        proxy_https_port=DEFAULT_PROXY_HTTPS_PORT,
        dns_host=DEFAULT_DNS_HOST,
        skip_ca_injection=False,
        skip_network_config=False,
        max_account_retries=3,
    ):
        self.adb_target = adb_target
        self.apk_path = Path(apk_path)
        self.phone = phone
        self.no_install = no_install
        self.proxy_host = proxy_host
        self.proxy_port = int(proxy_port)
        self.proxy_https_port = int(proxy_https_port)
        self.dns_host = dns_host
        self.skip_ca_injection = skip_ca_injection
        self.skip_network_config = skip_network_config
        self.max_account_retries = int(max_account_retries)
        self.account_retry_count = 0
        self.ocr_client = OcrClient()
        self.screenshots_dir = Path("/Users/one/Documents/home/redx/screenshots")
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = Path("/Users/one/Documents/home/redx/artifacts")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.capture_count = 0
        self.sms_submitted = False
        self.sms_code_was_submitted = False
        self.sms_submit_time = 0
        self.agreement_ticked = False
        self.loop_count = 0

    def run_adb(self, *args, check=True):
        cmd = ["adb", "-s", self.adb_target] + list(args)
        return run_cmd(cmd, check=check)

    def is_installed(self):
        stdout, _ = self.run_adb("shell", "pm", "path", XHS_PACKAGE, check=False)
        return "package:" in stdout

    def uninstall_app(self):
        log_info(f"Uninstalling {XHS_PACKAGE}...")
        stdout, stderr = self.run_adb("shell", "pm", "uninstall", XHS_PACKAGE, check=False)
        if "Success" in stdout:
            log_info("App uninstalled.")
            return
        log_warning(f"Uninstall did not report success: {stdout} {stderr}".strip())

    def clear_app_data(self):
        log_info(f"Clearing {XHS_PACKAGE} app data...")
        stdout, stderr = self.run_adb("shell", "pm", "clear", XHS_PACKAGE, check=False)
        if "Success" in stdout:
            log_success("App data cleared.")
        else:
            log_warning(f"pm clear did not report success: {stdout} {stderr}".strip())

    def install_app(self):
        if not self.apk_path.is_file():
            raise FileNotFoundError(f"APK not found at {self.apk_path}")
        log_info(f"Installing {self.apk_path.name} to {self.adb_target}...")
        cmd = ["adb", "-s", self.adb_target, "install", "-r", "-d", "-g", str(self.apk_path)]
        stdout, stderr = run_cmd(cmd)
        log_info(f"Install output: {stdout} {stderr}")
        log_success("App installed successfully.")

    def launch_app(self):
        log_info(f"Launching app {XHS_ACTIVITY}...")
        self.run_adb("shell", "am", "force-stop", XHS_PACKAGE)
        time.sleep(1)
        self.run_adb("shell", "am", "start", "-n", XHS_ACTIVITY)
        time.sleep(3)

    def reset_login_state_for_next_account(self):
        self.phone = None
        self.sms_submitted = False
        self.sms_code_was_submitted = False
        self.sms_submit_time = 0
        self.agreement_ticked = False
        self.clear_app_data()
        self.launch_app()

    def resolve_package_uid(self):
        stdout, _ = self.run_adb("shell", "stat", "-c", "'%u'", f"/data/data/{XHS_PACKAGE}")
        uid = stdout.strip().replace("'", "").replace('"', "")
        if not uid.isdigit():
            raise RuntimeError(f"Invalid package UID output: {stdout!r}")
        log_success(f"Resolved package UID: {uid}")
        return uid

    def install_burp_ca(self):
        if self.skip_ca_injection:
            log_info("Skipping Burp CA injection because --skip-ca-injection was specified.")
            return

        cert_url = f"http://{self.proxy_host}:{self.proxy_port}/cert"
        der_path = self.artifacts_dir / "burp_proxy_ca.der"
        pem_path = self.artifacts_dir / "burp_proxy_ca.pem"
        log_info(f"Downloading Burp CA from {cert_url}...")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(cert_url, timeout=15) as response:
            der_path.write_bytes(response.read())

        log_info("Converting Burp CA to Android system certificate format...")
        run_cmd(["openssl", "x509", "-inform", "DER", "-in", str(der_path), "-out", str(pem_path)])
        hash_stdout, _ = run_cmd(["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(pem_path), "-noout"])
        cert_hash = hash_stdout.splitlines()[0].strip()
        if not re.fullmatch(r"[0-9a-fA-F]{8}", cert_hash):
            raise RuntimeError(f"Invalid Android CA hash from openssl: {hash_stdout!r}")
        android_cert = self.artifacts_dir / f"{cert_hash}.0"
        copyfile(pem_path, android_cert)

        remote_dir = "/data/local/tmp/codex_burp_ca"
        remote_cert = f"{remote_dir}/{android_cert.name}"
        self.run_adb("shell", "mkdir", "-p", remote_dir)
        self.run_adb("push", str(android_cert), remote_cert)

        shell_script = f"""
set -eu
CERT_SRC='{remote_cert}'
CERT_HASH='{android_cert.name}'
LIVE_DIR='/data/local/tmp/codex_cacerts_live'
for pname in zygote zygote64 system_server {XHS_PACKAGE}; do
  for pid in $(pidof "$pname" 2>/dev/null || true); do
    nsenter -t "$pid" -m sh -c 'for p in /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts /system/apex/com.android.conscrypt/cacerts; do mountpoint -q "$p" && umount "$p" || true; done' 2>/dev/null || true
  done
done
for p in /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts /system/apex/com.android.conscrypt/cacerts; do
  mountpoint -q "$p" && umount "$p" || true
done
rm -rf "$LIVE_DIR"
mkdir -p "$LIVE_DIR"
cp /system/etc/security/cacerts/* "$LIVE_DIR"/
cp "$CERT_SRC" "$LIVE_DIR/$CERT_HASH"
chown root:root "$LIVE_DIR"/* 2>/dev/null || true
chmod 0644 "$LIVE_DIR"/*
chcon u:object_r:system_file:s0 "$LIVE_DIR"/* 2>/dev/null || true
if mountpoint -q /system/etc/security/cacerts; then umount /system/etc/security/cacerts || true; fi
mount -o bind "$LIVE_DIR" /system/etc/security/cacerts
if [ -d /apex/com.android.conscrypt/cacerts ]; then
  if mountpoint -q /apex/com.android.conscrypt/cacerts; then umount /apex/com.android.conscrypt/cacerts || true; fi
  mount -o bind "$LIVE_DIR" /apex/com.android.conscrypt/cacerts
fi
mkdir -p /data/misc/user/0/cacerts-added
cp "$CERT_SRC" "/data/misc/user/0/cacerts-added/$CERT_HASH" 2>/dev/null || true
chmod 0644 "/data/misc/user/0/cacerts-added/$CERT_HASH" 2>/dev/null || true
for pname in zygote zygote64 system_server {XHS_PACKAGE}; do
  for pid in $(pidof "$pname" 2>/dev/null || true); do
    nsenter -t "$pid" -m sh -c 'mountpoint -q /system/etc/security/cacerts && umount /system/etc/security/cacerts || true; mount -o bind /data/local/tmp/codex_cacerts_live /system/etc/security/cacerts' 2>/dev/null || true
    nsenter -t "$pid" -m sh -c '[ -d /apex/com.android.conscrypt/cacerts ] || exit 0; mountpoint -q /apex/com.android.conscrypt/cacerts && umount /apex/com.android.conscrypt/cacerts || true; mount -o bind /data/local/tmp/codex_cacerts_live /apex/com.android.conscrypt/cacerts' 2>/dev/null || true
  done
done
ls "/system/etc/security/cacerts/$CERT_HASH" >/dev/null
"""
        self.run_adb("shell", f"sh -c {shlex.quote(shell_script)}")
        log_success(f"Burp CA injected into Android trust store as {android_cert.name}.")

    def configure_transparent_proxy(self):
        if self.skip_network_config:
            log_info("Skipping iptables network configuration because --skip-network-config was specified.")
            return

        log_info("Setting up transparent Burp proxy rules...")
        uid = self.resolve_package_uid()

        rules_out, _ = self.run_adb("shell", "iptables", "-t", "nat", "-S", "OUTPUT", check=False)
        for line in rules_out.splitlines():
            if MITM_CHAIN_NAME in line:
                delete_args = line.replace("-A ", "-D ", 1).split()
                log_info(f"Removing old OUTPUT rule: {' '.join(delete_args)}")
                self.run_adb("shell", "iptables", "-t", "nat", *delete_args, check=False)

        self.run_adb("shell", "iptables", "-t", "nat", "-N", MITM_CHAIN_NAME, check=False)
        self.run_adb("shell", "iptables", "-t", "nat", "-F", MITM_CHAIN_NAME)
        self.run_adb("shell", "iptables", "-t", "nat", "-A", MITM_CHAIN_NAME, "-d", f"{self.proxy_host}/32", "-j", "RETURN")
        port_destinations = {
            "80": self.proxy_port,
            "443": self.proxy_https_port,
        }
        for port, destination_port in port_destinations.items():
            self.run_adb(
                "shell",
                "iptables",
                "-t",
                "nat",
                "-A",
                MITM_CHAIN_NAME,
                "-p",
                "tcp",
                "--dport",
                port,
                "-j",
                "DNAT",
                "--to-destination",
                f"{self.proxy_host}:{destination_port}",
            )
        self.run_adb("shell", "iptables", "-t", "nat", "-A", "OUTPUT", "-m", "owner", "--uid-owner", uid, "-j", MITM_CHAIN_NAME)
        log_success(
            f"iptables TCP redirect rules configured for UID {uid}: "
            f"80 -> {self.proxy_host}:{self.proxy_port}, 443 -> {self.proxy_host}:{self.proxy_https_port}."
        )

        dns_rule = f"udp -m udp --dport 53 -j DNAT --to-destination {self.dns_host}:53"
        dns_rules, _ = self.run_adb("shell", "iptables", "-t", "nat", "-S", "OUTPUT", check=False)
        if dns_rule not in dns_rules:
            self.run_adb("shell", "iptables", "-t", "nat", "-A", "OUTPUT", "-p", "udp", "--dport", "53", "-j", "DNAT", "--to-destination", f"{self.dns_host}:53")
            log_success(f"DNS DNAT rule added: {self.dns_host}:53.")
        else:
            log_info("DNS DNAT rule already exists, skipping.")

    def capture_screenshot(self, stage_name):
        self.capture_count += 1
        local_path = self.screenshots_dir / f"{self.capture_count:03d}_{stage_name}.png"
        log_info(f"Capturing screenshot: {local_path.name}...")
        cmd = ["adb", "-s", self.adb_target, "exec-out", "screencap", "-p"]
        with open(local_path, "wb") as f:
            subprocess.run(cmd, stdout=f, check=True)
        return local_path

    def get_ocr_items(self, image_path):
        log_info("Sending screenshot to OCR service...")
        try:
            return self.ocr_client.recognize_file(image_path)
        except Exception as e:
            log_error(f"OCR service failed: {e}")
            return []

    def tap_point(self, x, y, description="point"):
        log_info(f"Tapping {description} at ({x}, {y})")
        self.run_adb("shell", "input", "tap", str(x), str(y))
        time.sleep(1.5)

    def input_text(self, text):
        log_info(f"Typing text: {text}")
        self.run_adb("shell", "input", "text", text)
        time.sleep(1.0)

    def clear_text_field(self):
        log_info("Clearing text field...")
        self.run_adb("shell", "input", "keycombination", "113", "29")
        time.sleep(0.2)
        self.run_adb("shell", "input", "keyevent", "67")  # Backspace
        time.sleep(0.5)
        self.run_adb("shell", "input", "keyevent", "123")  # Move cursor to end
        for _ in range(16):
            self.run_adb("shell", "input", "keyevent", "67")
        time.sleep(0.5)

    def find_text(self, items, *keywords, min_conf=0.75):
        for item in items:
            if item.conf < min_conf:
                continue
            text_normalized = item.text.replace(" ", "").replace("\n", "")
            for keyword in keywords:
                keyword_normalized = keyword.replace(" ", "")
                if keyword_normalized in text_normalized:
                    return item
        return None

    def detect_logged_in_homepage(self, items):
        has_me_tab = False
        has_msg_tab = False
        has_home_tab = False
        
        for item in items:
            if item.conf < 0.75:
                continue
            text = item.text.strip().replace(" ", "")
            cx, cy = item.center
            
            # y > 2150 (bottom navigation bar)
            if cy > 2150:
                if text == "我" or (len(text) <= 3 and "我" in text):
                    if cx > 750:
                        has_me_tab = True
                elif text == "消息" or (len(text) <= 4 and "消息" in text):
                    if 550 < cx < 850:
                        has_msg_tab = True
                elif text == "首页" or (len(text) <= 4 and "首页" in text):
                    if cx < 300:
                        has_home_tab = True
                        
        login_indicators = [
            "手机号登录", "验证并登录", "请输入手机号", "获取验证码", 
            "其他登录方式", "去登录", "立即登录", "登录查看更多", 
            "登录后发现", "同意后登录", "勾选同意后", "账号已退出登录",
            "账号下线提示", "重新登录", "账号封禁说明", "账号存在违规行为",
            "无法继续使用"
        ]
        has_login_button = self.find_text(items, *login_indicators, min_conf=0.7) is not None
        
        # Log bottom bar detection details if any matched
        if has_me_tab or has_msg_tab or has_home_tab:
            log_info(f"Bottom bar check: me={has_me_tab}, msg={has_msg_tab}, home={has_home_tab}, has_login={has_login_button}")
            
        if has_me_tab and (has_msg_tab or has_home_tab) and not has_login_button:
            return True
        return False

    def detect_account_blocker(self, items):
        risk_tip = self.find_text(items, "账号封禁说明", "账号存在违规行为", "无法继续使用", min_conf=0.75)
        if risk_tip:
            return "risk_intercept"
        offline_tip = self.find_text(items, "账号已退出登录", "账号下线提示", "重新登录", min_conf=0.75)
        if offline_tip:
            return "account_offline"
        return None

    def handle_account_blocker(self, blocker_name):
        self.capture_screenshot(blocker_name)
        if self.account_retry_count < self.max_account_retries:
            self.account_retry_count += 1
            log_warning(
                f"Detected {blocker_name}; retrying with a fresh phone "
                f"({self.account_retry_count}/{self.max_account_retries})."
            )
            self.reset_login_state_for_next_account()
            return True
        log_error(f"Detected {blocker_name} and max account retries were exhausted.")
        return False

    def start_login_loop(self):
        log_info("Starting simulated login loop...")
        
        consecutive_unknowns = 0
        sms_sent = False

        while True:
            if consecutive_unknowns > 15:
                log_error("Too many consecutive unknown screens. Exiting.")
                break

            screenshot_path = self.capture_screenshot("loop")
            items = self.get_ocr_items(screenshot_path)
            self.loop_count += 1
            agreement_text = self.find_text(items, "我已阅读并同意", "已阅读并同意", "已阅读同意", "同意《用户协议", min_conf=0.75)
            
            # If SMS was submitted, check if we need to reset for retry (e.g. after 30 seconds)
            if self.sms_submitted and (time.time() - self.sms_submit_time > 30):
                log_warning("SMS submitted over 30s ago but still not logged in. Resetting SMS submitted state for retry.")
                self.sms_submitted = False
            
            detected_texts = [item.text for item in items]
            log_info(f"OCR detected texts: {detected_texts[:15]}... ({len(detected_texts)} items total)")

            account_blocker = self.detect_account_blocker(items)
            if account_blocker:
                if self.handle_account_blocker(account_blocker):
                    sms_sent = False
                    consecutive_unknowns = 0
                    continue
                break

            notification_permission = self.find_text(items, "向您发送通知", "通知权限", min_conf=0.75)
            permission_deny_btn = None
            permission_allow_btn = None
            for item in items:
                if item.conf < 0.8:
                    continue
                text_norm = item.text.strip().replace(" ", "")
                if text_norm == "不允许":
                    permission_deny_btn = item
                elif text_norm == "允许":
                    permission_allow_btn = item
            if notification_permission and (permission_deny_btn or permission_allow_btn):
                target = permission_deny_btn or permission_allow_btn
                log_info("Detected Android notification permission popup.")
                self.tap_point(target.center[0], target.center[1], "Notification Permission Button")
                consecutive_unknowns = 0
                continue

            in_app_notification = self.find_text(items, "打开通知", "热门笔记和互动", "消息第一时间通知", min_conf=0.75)
            in_app_confirm = self.find_text(items, "确认", min_conf=0.75)
            if in_app_notification and in_app_confirm:
                log_info("Detected in-app notification prompt.")
                self.tap_point(in_app_confirm.center[0], in_app_confirm.center[1], "In-app Notification Confirm Button")
                consecutive_unknowns = 0
                continue

            # Check for Captcha / Slide verification
            captcha_item = self.find_text(items, "安全验证", "拖动滑块", "向右滑动", "完成验证", "请完成验证", "验证码发送频繁")
            if captcha_item:
                log_warning("----------------------------------------------------------------")
                log_warning("DETECTED CAPTCHA OR SLIDE VERIFICATION!")
                log_warning("Please manually solve the captcha in the container or screen cast.")
                log_warning("After you solve the captcha, press ENTER here to continue...")
                log_warning("----------------------------------------------------------------")
                input()
                consecutive_unknowns = 0
                continue

            # Check for Privacy Agreement Popup
            agree_btn = self.find_text(items, "同意并继续", "同意并使用", min_conf=0.8)
            if not agree_btn:
                for item in items:
                    if item.conf >= 0.75:
                        text_norm = item.text.replace(" ", "")
                        if text_norm in ("同意", "我同意", "同意并继续", "同意并使用"):
                            agree_btn = item
                            break
            if agree_btn:
                log_info("Found privacy policy consent popup.")
                self.tap_point(agree_btn.center[0], agree_btn.center[1], "Consent Agreement Button")
                consecutive_unknowns = 0
                continue

            # Check for guest mode "Go to login" triggers
            go_login_btn = self.find_text(items, "去登录", "去注册", "立即登录", "登录查看更多", min_conf=0.75)
            if go_login_btn:
                log_info("Detected guest mode login button, tapping it to go to login page...")
                self.tap_point(go_login_btn.center[0], go_login_btn.center[1], "Go to Login Button")
                consecutive_unknowns = 0
                continue

            # Check if we are on XHS Main Page (Logged In)
            if self.detect_logged_in_homepage(items):
                if self.loop_count <= 2 or self.sms_code_was_submitted:
                    time.sleep(2)
                    verify_path = self.capture_screenshot("success_verify")
                    verify_items = self.get_ocr_items(verify_path)
                    verify_blocker = self.detect_account_blocker(verify_items)
                    if verify_blocker:
                        if self.handle_account_blocker(verify_blocker):
                            sms_sent = False
                            consecutive_unknowns = 0
                            continue
                        break
                    log_success("================================================================")
                    log_success("SUCCESSFULLY LOGGED IN! Bottom navigation bar verified.")
                    log_success("================================================================")
                    self.capture_screenshot("success")
                    break
                else:
                    log_info("Logged in homepage layout detected, but SMS code was not submitted yet in this run. Skipping success trigger.")

            # Check welcome page (Login Selection Page)
            phone_login_btn = self.find_text(items, "手机号登录", "手机号一键登录", "手机号安全登录", min_conf=0.8)
            other_login_btn = self.find_text(items, "其他登录方式", "其他手机号登录", min_conf=0.8)

            if other_login_btn:
                log_info("Found 'Other login methods' button.")
                self.tap_point(other_login_btn.center[0], other_login_btn.center[1], "Other Login Methods")
                consecutive_unknowns = 0
                continue
            
            is_phone_entry_screen = self.find_text(items, "验证并登录", "密码登录", "未注册的手机号登录成功后将自动注册", min_conf=0.75) is not None
            if phone_login_btn and not is_phone_entry_screen and not self.find_text(items, "请输入手机号", "获取验证码", min_conf=0.75):
                if agreement_text and not self.agreement_ticked:
                    left_x = agreement_text.coords[0][0]
                    checkbox_x = max(20, left_x - 45) 
                    checkbox_y = agreement_text.center[1]
                    log_info("Ticking privacy agreement checkbox on welcome page...")
                    self.tap_point(checkbox_x, checkbox_y, "Agreement Checkbox (Left of Text)")
                    self.agreement_ticked = True
                    time.sleep(0.5)
                log_info("Found initial 'Phone Login' button.")
                self.tap_point(phone_login_btn.center[0], phone_login_btn.center[1], "Phone Login Button")
                consecutive_unknowns = 0
                continue

            # Explicitly detect if we are on the SMS page to prevent phone page keyword collisions
            is_sms_page = self.find_text(items, "输入验证码", "验证码已发送", "秒后重新获取", "重新发送", "验证码", min_conf=0.75) is not None

            # Check for Phone & Code input page
            phone_prompt = self.find_text(items, "请输入手机号", "请输入手机号码", "输入手机号", min_conf=0.75)
            plus_86 = self.find_text(items, "+86", "+86>", min_conf=0.75)
            
            if (phone_prompt or plus_86) and not sms_sent and not is_sms_page:
                consecutive_unknowns = 0
                
                # 1. Tick agreement checkbox
                if agreement_text and not self.agreement_ticked:
                    left_x = agreement_text.coords[0][0]
                    checkbox_x = max(20, left_x - 45) 
                    checkbox_y = agreement_text.center[1]
                    log_info("Ticking privacy agreement checkbox...")
                    self.tap_point(checkbox_x, checkbox_y, "Agreement Checkbox (Left of Text)")
                    self.agreement_ticked = True
                    time.sleep(0.5)

                # 2. Fetch and Input Phone Number
                if not self.phone:
                    self.phone = fetch_phone_from_api()
                    if not self.phone:
                        log_warning("API phone fetch failed. Falling back to terminal input...")
                        self.phone = input("Please enter your phone number (11 digits): ").strip()

                target_field = plus_86 or phone_prompt
                if target_field:
                    if plus_86:
                        input_x = plus_86.center[0] + 160
                        input_y = plus_86.center[1]
                    else:
                        input_x = phone_prompt.center[0]
                        input_y = phone_prompt.center[1]
                    
                    self.tap_point(input_x, input_y, "Phone Input Field")
                    self.clear_text_field()
                    self.input_text(self.phone)
                
                # 3. Click "Get Verification Code"
                get_code_btn = self.find_text(items, "获取验证码", "发送验证码", "下一步", "验证并登录", min_conf=0.8)
                if get_code_btn:
                    log_info("Clicking 'Get Verification Code' button...")
                    self.tap_point(get_code_btn.center[0], get_code_btn.center[1], "Get Code Button")
                    sms_sent = True
                    consecutive_unknowns = 0
                else:
                    log_warning("Phone entered, but 'Get Code' button not found on screen yet.")
                continue

            # Check if we are on SMS Verification Code input page
            if is_sms_page and not self.sms_submitted:
                log_info("Detected SMS verification code entry page. Starting API polling...")
                
                # Poll SMS code from API
                code = None
                poll_attempts = 24  # 24 * 5s = 120s
                for attempt in range(1, poll_attempts + 1):
                    code = fetch_sms_code_from_api(self.phone)
                    if code:
                        log_success(f"Successfully retrieved SMS verification code: {code}")
                        break
                    log_info(f"[{attempt}/{poll_attempts}] No SMS code yet, waiting 5 seconds...")
                    time.sleep(5)
                
                if not code:
                    log_warning("API polling timed out. Falling back to terminal input...")
                    log_warning("----------------------------------------------------------------")
                    log_warning("Please enter the SMS code or type it manually on the screen:")
                    log_warning("----------------------------------------------------------------")
                    code = input("Verification Code (leave blank if typed manually on phone): ").strip()
                
                if code:
                    sms_input_field = self.find_text(items, "输入验证码", "验证码", min_conf=0.75)
                    if sms_input_field:
                        self.tap_point(sms_input_field.center[0], sms_input_field.center[1], "SMS Code Box area")
                    else:
                        self.tap_point(540, 600, "Fallback Screen Center for input focus")
                    self.input_text(code)
                    login_submit_btn = None
                    for item in items:
                        if item.conf < 0.75:
                            continue
                        text_norm = item.text.strip().replace(" ", "")
                        if text_norm in ("登录", "验证并登录"):
                            login_submit_btn = item
                            break
                    if login_submit_btn:
                        self.tap_point(login_submit_btn.center[0], login_submit_btn.center[1], "Login Submit Button")
                    else:
                        self.tap_point(540, 950, "Fallback Login Submit Button")
                    self.sms_submitted = True
                    self.sms_code_was_submitted = True
                    self.sms_submit_time = time.time()
                
                time.sleep(3)
                sms_sent = False
                consecutive_unknowns = 0
                continue

            if is_sms_page and self.sms_submitted:
                log_info("SMS code has been submitted. Waiting for transition to homepage...")
                consecutive_unknowns = 0
                time.sleep(2)
                continue

            log_warning("Screen state does not match any known login pages. Waiting...")
            consecutive_unknowns += 1
            time.sleep(2)

def main():
    parser = argparse.ArgumentParser(description="Xiaohongshu simulated login script")
    parser.add_argument("--phone", type=str, help="XHS phone number to login")
    parser.add_argument("--adb-target", type=str, default=DEFAULT_ADB_TARGET, help="ADB device target string")
    parser.add_argument("--apk-path", type=str, default=DEFAULT_APK_PATH, help="Path to XHS APK file")
    parser.add_argument("--no-install", action="store_true", help="Skip uninstalling and reinstalling the app")
    parser.add_argument("--proxy-host", type=str, default=DEFAULT_PROXY_HOST, help="Burp transparent proxy host reachable from Android")
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT, help="Burp transparent proxy port")
    parser.add_argument("--proxy-https-port", type=int, default=DEFAULT_PROXY_HTTPS_PORT, help="Burp transparent proxy port for HTTPS traffic")
    parser.add_argument("--dns-host", type=str, default=DEFAULT_DNS_HOST, help="DNS server used by Android UDP/53 DNAT")
    parser.add_argument("--skip-ca-injection", action="store_true", help="Do not download and inject the Burp CA")
    parser.add_argument("--skip-network-config", action="store_true", help="Do not configure iptables transparent proxy rules")
    parser.add_argument("--max-account-retries", type=int, default=3, help="Retry with a fresh phone when a risk/intercepted account is detected")
    args = parser.parse_args()

    runner = XhsLoginRunner(
        adb_target=args.adb_target,
        apk_path=args.apk_path,
        phone=args.phone,
        no_install=args.no_install,
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
        proxy_https_port=args.proxy_https_port,
        dns_host=args.dns_host,
        skip_ca_injection=args.skip_ca_injection,
        skip_network_config=args.skip_network_config,
        max_account_retries=args.max_account_retries,
    )

    try:
        log_info(f"Checking device {runner.adb_target} connection...")
        stdout, _ = runner.run_adb("devices")
        if runner.adb_target not in stdout:
            log_error(f"Device {runner.adb_target} not found in adb devices list. Please check connection.")
            sys.exit(1)
        log_success("ADB connection verified.")

        if not runner.no_install:
            runner.uninstall_app()
            runner.install_app()
            runner.clear_app_data()
        else:
            log_info("Skipping install because --no-install was specified.")
            if not runner.is_installed():
                log_warning("App is not installed. Installing it anyway...")
                runner.install_app()
                runner.clear_app_data()

        runner.install_burp_ca()
        runner.configure_transparent_proxy()

        runner.launch_app()
        runner.start_login_loop()

    except KeyboardInterrupt:
        log_warning("\nScript terminated by user.")
    except Exception as e:
        log_error(f"Execution failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
