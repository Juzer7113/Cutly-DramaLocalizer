#!/usr/bin/env python3
"""安全部署 Cutly：暂存、校验、备份、原子替换、重启和鉴权健康检查。"""
import json
import os
import shlex
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import paramiko

# 部署目标不是应用运行配置：为避免迁移后残留旧 IP，必须由环境变量明确指定。
HOST = os.getenv("DRAMA_HOST", "").strip()
USER = os.getenv("DRAMA_USER", "juzer")
PW = os.getenv("DRAMA_SSH_PASSWORD", "")
REMOTE = os.getenv("DRAMA_REMOTE", "/www/wwwroot/aidj/drama_localizer")
LOCAL = Path(__file__).resolve().parent
APP_KEY = os.getenv("WORKSTATION_API_KEY", "").strip()
FILES = ["workstation.py", "pipeline.py", "static/index.html", "fetch_fonts.sh", "run.sh",
         "requirements.txt", ".env.example"]
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}


def deploy_files():
    """源码文件加随源码保存的离线字体；manifest 供前端显示字体名称。"""
    font_dir = LOCAL / "fonts"
    fonts = ([f"fonts/{p.name}" for p in sorted(font_dir.iterdir())
              if p.is_file() and (p.suffix.lower() in FONT_EXTS or p.name == "manifest.json")]
             if font_dir.is_dir() else [])
    return FILES + fonts


def ssh():
    if not HOST:
        raise RuntimeError("请先设置 DRAMA_HOST，例如：export DRAMA_HOST=172.16.1.24")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    kwargs = {"hostname": HOST, "username": USER, "timeout": 20, "look_for_keys": True}
    if PW:
        kwargs["password"] = PW
    client.connect(**kwargs)
    return client


def run(client, command, timeout=120, check=True):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if check and code:
        raise RuntimeError(f"远端命令失败({code}): {err or out}")
    return out


def ensure_remote_env(client):
    """通过 SFTP 更新密钥，避免把密钥插入 shell 命令或进程参数。"""
    if len(APP_KEY) < 8:
        raise RuntimeError("WORKSTATION_API_KEY 至少需要 8 个字符")
    sftp = client.open_sftp()
    path = f"{REMOTE}/.env"
    try:
        with sftp.open(path, "r") as f:
            lines = f.read().decode("utf-8").splitlines()
    except OSError:
        lines = []
    kept = [line for line in lines if not line.startswith("WORKSTATION_API_KEY=")]
    kept.append(f"WORKSTATION_API_KEY={APP_KEY}")
    with sftp.open(path, "w") as f:
        f.write(("\n".join(kept) + "\n").encode("utf-8"))
        f.chmod(0o600)
    sftp.close()


def restart_service(client):
    command = "sudo -S systemctl restart drama_localizer" if PW else "sudo -n systemctl restart drama_localizer"
    stdin, stdout, stderr = client.exec_command(command, get_pty=bool(PW), timeout=60)
    if PW:
        stdin.write(PW + "\n")
        stdin.flush()
    code = stdout.channel.recv_exit_status()
    if code:
        raise RuntimeError(stderr.read().decode(errors="replace").strip() or "服务重启失败")


def health_check():
    base = f"http://{HOST}:8000"
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    payload = json.dumps({"key": APP_KEY}).encode()
    req = urllib.request.Request(base + "/d/api/auth/login", data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("登录健康检查失败")
    for path in ("/d/", "/d/api/version", "/d/api/jobs", "/d/api/fonts"):
        with opener.open(base + path, timeout=15) as response:
            print(f"GET {path} -> {response.status}")


def main():
    client = ssh()
    files = deploy_files()
    release = time.strftime("%Y%m%d%H%M%S")
    stage = f"{REMOTE}/.deploy-{release}"
    backup = f"{REMOTE}/backups/{release}"
    run(client, "mkdir -p " + " ".join(shlex.quote(path) for path in (
        stage + "/static", stage + "/fonts", backup + "/static", backup + "/fonts")))
    sftp = client.open_sftp()
    try:
        for relative in files:
            sftp.put(str(LOCAL / relative), f"{stage}/{relative}")
    finally:
        sftp.close()
    run(client, f"cd {shlex.quote(stage)} && {shlex.quote(REMOTE + '/venv/bin/python')} -m py_compile workstation.py pipeline.py")
    ensure_remote_env(client)
    quoted_files = " ".join(shlex.quote(f) for f in files)
    # 新增字体首次部署时运行目录中可能还不存在，因此备份仅复制已有文件。
    backup_cmd = " ".join(
        f"if [ -e {shlex.quote(f)} ]; then cp --parents {shlex.quote(f)} {shlex.quote(backup)}; fi;"
        for f in files)
    run(client, f"cd {shlex.quote(REMOTE)} && {backup_cmd}")
    run(client, f"cd {shlex.quote(stage)} && cp --parents {quoted_files} {shlex.quote(REMOTE)}")
    font_files = [f for f in files if Path(f).suffix.lower() in FONT_EXTS]
    if font_files:
        quoted_fonts = " ".join(shlex.quote(f"{REMOTE}/{f}") for f in font_files)
        run(client, f"mkdir -p /home/{shlex.quote(USER)}/.fonts && "
                    f"cp -f {quoted_fonts} /home/{shlex.quote(USER)}/.fonts/ && fc-cache -f")
    restart_service(client)
    client.close()
    time.sleep(5)
    health_check()
    print(f"部署完成；回滚备份：{backup}")


if __name__ == "__main__":
    main()
