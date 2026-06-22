#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="root@47.103.84.150"
REMOTE_DIR="/opt/xhs-fetcher"
PEM_PATH="/home/pem/waicai-shanghai.pem"

ssh -i "${PEM_PATH}" -o StrictHostKeyChecking=no "${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}"
scp -i "${PEM_PATH}" -o StrictHostKeyChecking=no /home/redx/xhs_fetcher.py /home/redx/requirements.txt "${REMOTE_HOST}:${REMOTE_DIR}/"

ssh -i "${PEM_PATH}" -o StrictHostKeyChecking=no "${REMOTE_HOST}" <<'EOF'
set -euo pipefail
apt-get update
apt-get install -y python3 python3-pip
mkdir -p /opt/xhs-fetcher/logs
python3 -m pip install --break-system-packages -r /opt/xhs-fetcher/requirements.txt
cat >/etc/systemd/system/xhs-fetcher.service <<'SERVICE'
[Unit]
Description=XHS URL Fetcher
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/opt/xhs-fetcher
Environment="DB_HOST=qq.rwlb.rds.aliyuncs.com"
Environment="DB_USER=data"
Environment="DB_PASSWORD=AbHGL8jMwMPmzM"
Environment="DB_NAME=data"
Environment="DB_PORT=3306"
ExecStart=/usr/bin/python3 /opt/xhs-fetcher/xhs_fetcher.py --log-level INFO
StandardOutput=append:/opt/xhs-fetcher/logs/fetcher.log
StandardError=append:/opt/xhs-fetcher/logs/fetcher.log

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/xhs-fetcher.timer <<'TIMER'
[Unit]
Description=Run XHS URL Fetcher every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit=xhs-fetcher.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now xhs-fetcher.timer
systemctl start xhs-fetcher.service
systemctl status xhs-fetcher.service --no-pager || true
systemctl status xhs-fetcher.timer --no-pager || true
EOF
