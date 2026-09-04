#!/usr/bin/env bash
# 在新服务器上从当前目录安装/更新 Cutly 服务。
set -euo pipefail
BASE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
RUN_DIR="$BASE_DIR/drama_localizer"
SRC_DIR="$BASE_DIR/drama_localizer_ym"
SERVICE_FILE=/etc/systemd/system/drama_localizer.service

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 或 sudo 执行：sudo bash $0" >&2
  exit 1
fi
for path in "$RUN_DIR/run.sh" "$RUN_DIR/venv/bin/python" "$RUN_DIR/workstation.py" "$SRC_DIR/models"; do
  [ -e "$path" ] || { echo "缺少必要文件：$path" >&2; exit 1; }
done

# Python venv 中的命令脚本带有创建时的绝对路径；目录迁移后修正它们，
# 避免直接执行 demucs/pip 等命令时仍指向旧服务器目录。不会重新下载或重装依赖。
VENV_DIR="$RUN_DIR/venv"
OLD_VENV_DIR="$(sed -n 's#^command = .* -m venv ##p' "$VENV_DIR/pyvenv.cfg" 2>/dev/null || true)"
if [ -n "$OLD_VENV_DIR" ] && [ "$OLD_VENV_DIR" != "$VENV_DIR" ]; then
  find "$VENV_DIR/bin" -maxdepth 1 -type f -exec sed -i "s|$OLD_VENV_DIR|$VENV_DIR|g" {} +
  sed -i "s|^command = .*|command = /usr/bin/python3 -m venv $VENV_DIR|" "$VENV_DIR/pyvenv.cfg"
fi

SERVICE_USER="${CUTLY_USER:-$(stat -c '%U' "$RUN_DIR")}"
[ "$SERVICE_USER" = root ] && SERVICE_USER="${SUDO_USER:-$(id -un)}"
id "$SERVICE_USER" >/dev/null 2>&1 || { echo "用户不存在：$SERVICE_USER；可用 CUTLY_USER 指定" >&2; exit 1; }

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$RUN_DIR/output/jobs" "$RUN_DIR/fonts"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "/home/$SERVICE_USER/.fonts"
find "$SRC_DIR/fonts" -maxdepth 1 -type f \( -name '*.ttf' -o -name '*.otf' -o -name '*.woff' -o -name '*.woff2' \) -exec cp -f {} "/home/$SERVICE_USER/.fonts/" \;
chown -R "$SERVICE_USER:$SERVICE_USER" "/home/$SERVICE_USER/.fonts"
fc-cache -f

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Cutly AI Video Localization Workbench
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$RUN_DIR
ExecStart=$RUN_DIR/run.sh
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$RUN_DIR/.env
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
KillMode=mixed
TimeoutStopSec=30
StandardOutput=append:$RUN_DIR/workstation.log
StandardError=append:$RUN_DIR/workstation.log

[Install]
WantedBy=multi-user.target
EOF

chmod +x "$RUN_DIR/run.sh"
systemctl daemon-reload
systemctl enable --now drama_localizer.service
systemctl --no-pager --full status drama_localizer.service | sed -n '1,22p'
echo "Cutly 已安装：$RUN_DIR"
