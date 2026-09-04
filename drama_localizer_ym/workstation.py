#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文短剧 -> 多语种 本地化工作台 (Web 服务)  v2 分步式

人在环工作流:
  ① 上传视频          -> 建任务 (状态 uploaded)
  ② 点「识别中文字幕」 -> Whisper 识别中文 -> 时间轴 (状态 asr_done)
  ③ 人工精修          -> 改错别字 / 加 [w]=女声 [m]=男声 (状态 refined)
  ④ 点「生成配音」     -> 翻译(目标语种) + 按性别选 voice + edge-tts + strict 时长对齐 (状态 dubbed)
  ⑤ 点「去除原人人声」 -> demucs 剥离人声保留配乐 (加分项, vocals_removed)
  ⑥ 点「导出视频」     -> 合成 视频 + (配乐+配音) + 字幕 (状态 exported)

目标语言接口化: 默认 km(高棉语)，前端可切 th/en/vi/...，仅需对应 voice 映射。

启动:
  cd <运行目录> && ./venv/bin/python -m uvicorn workstation:app --host 0.0.0.0 --port 8000
"""
import asyncio
import copy
import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

import edge_tts
from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

import pipeline  # 复用已验证的本地化引擎

# 前端版本号（与 static/index.html 中的 APP_VER 保持一致；改 JS 时同步 +1）
APP_VER = "v20260904015"

BASE = Path(__file__).resolve().parent
JOBS_DIR = Path(os.getenv("DRAMA_JOBS_DIR", str(BASE / "output" / "jobs"))).resolve()
JOBS_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR = BASE / "fonts"          # 用户可把 .ttf/.otf/.woff(2) 丢进这里，自动出现在字体下拉框
FONTS_DIR.mkdir(parents=True, exist_ok=True)
FONT_EXTS = (".ttf", ".otf", ".woff", ".woff2")
JOBS_FILE = JOBS_DIR / "jobs.json"
JOBS_BACKUP_FILE = JOBS_DIR / "jobs.json.bak"
PROJECTS_FILE = JOBS_DIR / "projects.json"
LOCK = threading.Lock()
TASK_CONTEXT = threading.local()
jobs: dict = {}
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "4096")) * 1024 * 1024
WORKSTATION_API_KEY = os.getenv("WORKSTATION_API_KEY", "").strip()
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
AUTH_COOKIE = "cutly_auth"

# ---------- 语言 / voice 配置（接口化，以后不止高棉语） ----------
TARGET_LANG = os.getenv("TARGET_LANG", "km").lower()
DEFAULT_GENDER = os.getenv("DEFAULT_GENDER", "female").lower()

LANG_NAME = {
    "my": "缅甸语", "en": "英语", "fil": "菲律宾语", "id": "印尼语", "jv": "爪哇语",
    "km": "高棉语", "lo": "老挝语", "ms": "马来语", "su": "巽他语",
    "ta": "泰米尔语", "th": "泰语", "vi": "越南语", "zh": "中文",
}
# 每个语种配 女声/男声 神经语音（微软 Edge 免费端点，免 key）
VOICE_MAP = {
    "zh": {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural"},
    "km": {"female": "km-KH-SreymomNeural", "male": "km-KH-PisethNeural"},
    "my": {"female": "my-MM-NilarNeural", "male": "my-MM-ThihaNeural"},
    "en": {"female": "en-US-AriaNeural", "male": "en-US-GuyNeural"},
    "fil": {"female": "fil-PH-BlessicaNeural", "male": "fil-PH-AngeloNeural"},
    "id": {"female": "id-ID-GadisNeural", "male": "id-ID-ArdiNeural"},
    "jv": {"female": "jv-ID-SitiNeural", "male": "jv-ID-DimasNeural"},
    "lo": {"female": "lo-LA-KeomanyNeural", "male": "lo-LA-ChanthavongNeural"},
    "ms": {"female": "ms-MY-YasminNeural", "male": "ms-MY-OsmanNeural"},
    "su": {"female": "su-ID-TutiNeural", "male": "su-ID-JajangNeural"},
    "ta": {"female": "ta-IN-PallaviNeural", "male": "ta-IN-ValluvarNeural"},
    "th": {"female": "th-TH-PremwadeeNeural", "male": "th-TH-NiwatNeural"},
    "vi": {"female": "vi-VN-HoaiMyNeural", "male": "vi-VN-NamMinhNeural"},
}

# 旧 [w]/[m]/[n] 标记持续兼容；新增声线仅改变音调，不伪称新的真人发音人。
GENDER_RE = re.compile(r"^\s*\[(w2|女2|f2|female2|m2|男2|male2|n2|旁白2|narrator2|w|女|f|female|m|男|male|n|旁白|narrator)\]", re.IGNORECASE)


def parse_gender(text: str, default_gender: str = DEFAULT_GENDER):
    m = GENDER_RE.match(text or "")
    if not m:
        allowed = ("female", "female2", "male", "male2", "narration", "narration2")
        return (default_gender if default_gender in allowed else DEFAULT_GENDER), text
    tag = m.group(1).lower()
    if tag in ("w2", "女2", "f2", "female2"):
        gender = "female2"
    elif tag in ("w", "女", "f", "female"):
        gender = "female"
    elif tag in ("m2", "男2", "male2"):
        gender = "male2"
    elif tag in ("m", "男", "male"):
        gender = "male"
    elif tag in ("n2", "旁白2", "narrator2"):
        gender = "narration2"
    else:
        gender = "narration"
    return gender, text[m.end():].strip()


def voice_tag(gender: str) -> str:
    return {"female": "[w]", "female2": "[w2]", "male": "[m]", "male2": "[m2]",
            "narration": "[n]", "narration2": "[n2]"}.get(gender, "[w]")


def cue_voice(cue: dict, default_gender: str = DEFAULT_GENDER) -> str:
    voice = cue.get("voice")
    if voice in ("female", "female2", "male", "male2", "narration", "narration2"):
        return voice
    return parse_gender(cue.get("text", ""), default_gender)[0]


def tts_voice_profile(lang: str, gender: str):
    """返回 Edge 声音及韵律；所有声线保持原速，仅用音调区分。"""
    voice_map = VOICE_MAP.get(lang, VOICE_MAP[TARGET_LANG])
    profiles = {
        "female": ("female", "+0%", "+0Hz"),
        "female2": ("female", "+0%", "+12Hz"),
        "male": ("male", "+0%", "+0Hz"),
        "male2": ("male", "+0%", "-12Hz"),
        "narration": ("male", "+0%", "-8Hz"),
        "narration2": ("female", "+0%", "-10Hz"),
    }
    source, rate, pitch = profiles.get(gender, profiles["female"])
    return voice_map.get(source, voice_map[DEFAULT_GENDER]), rate, pitch


def _load_jobs():
    for source in (JOBS_FILE, JOBS_BACKUP_FILE):
        if not source.exists():
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("任务库根节点必须是对象")
            jobs.update(data)
            return
        except (OSError, ValueError, json.JSONDecodeError) as ex:
            pipeline.log(f"任务库读取失败 {source}: {ex}")


def _save_jobs():
    """原子保存任务库，并保留上一份有效快照，避免进程中断导致全库损坏。"""
    payload = json.dumps(jobs, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".jobs.", suffix=".tmp", dir=JOBS_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp = Path(tmp_name)
        if JOBS_FILE.exists():
            try:
                current = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    shutil.copy2(JOBS_FILE, JOBS_BACKUP_FILE)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        os.replace(tmp, JOBS_FILE)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_json_write(path: Path, value: dict):
    """原子写项目索引/项目文件；项目媒体从不复制或覆盖。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _project_file_inventory(jid: str):
    """项目目录是媒体唯一实体位置；清单仅记录关联，不复制大文件。"""
    root = (JOBS_DIR / jid).resolve()
    files = []
    if not root.exists():
        return files
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.name == "project.json":
            continue
        try:
            rel = item.relative_to(root).as_posix()
            files.append({"path": rel, "bytes": item.stat().st_size, "kind": _media_kind(rel)
                          if Path(rel).suffix.lower() in VIDEO_EXTS + AUDIO_EXTS + SUB_EXTS + IMG_EXTS else "derived"})
        except (OSError, ValueError):
            continue
    return files


def _read_projects_index():
    try:
        value = json.loads(PROJECTS_FILE.read_text(encoding="utf-8")) if PROJECTS_FILE.exists() else {"projects": {}}
        return value if isinstance(value, dict) and isinstance(value.get("projects"), dict) else {"projects": {}}
    except (OSError, ValueError, json.JSONDecodeError) as ex:
        pipeline.log(f"项目库读取失败 {PROJECTS_FILE}: {ex}")
        return {"projects": {}}


def _save_project_locked(job):
    """把当前 job 快照写入其独立目录，并更新项目库索引。必须在 LOCK 内调用。"""
    jid = str(job["id"])
    now = time.time()
    jd = JOBS_DIR / jid
    jd.mkdir(parents=True, exist_ok=True)
    title = str(job.get("title") or "未命名项目").strip()[:180] or "未命名项目"
    job["title"] = title
    job["project_saved_at"] = now
    job["project_file"] = f"/d/files/{jid}/project.json"
    files = _project_file_inventory(jid)
    snapshot = copy.deepcopy(job)
    snapshot.pop("_task", None)  # 运行态任务不能作为可恢复项目状态保存
    manifest = {
        "schema": 1, "project_id": jid, "title": title, "saved_at": now,
        "project_folder": jid, "media_files": files, "job": snapshot,
    }
    _atomic_json_write(jd / "project.json", manifest)
    index = _read_projects_index()
    old = index["projects"].get(jid) or {}
    index["projects"][jid] = {
        "id": jid, "title": title, "status": job.get("status", "uploaded"),
        "saved_at": old.get("saved_at", now), "updated_at": now,
        "project_folder": jid, "project_file": f"/d/files/{jid}/project.json",
        "media_count": len(job.get("media") or []), "file_count": len(files),
        "media": [{"name": str(m.get("name") or "媒体"), "kind": m.get("kind", "")}
                  for m in (job.get("media") or [])],
        # 仅由项目库批量操作写入；不从任务状态推断，避免和编辑页的单项目操作混淆。
        "batch_results": copy.deepcopy(job.get("project_batch_results") or {}),
        "promo_copy": copy.deepcopy(job.get("promo_copy") or {}),
    }
    _atomic_json_write(PROJECTS_FILE, index)
    return copy.deepcopy(index["projects"][jid])


def _recover_jobs():
    """进程重启后，原来 running 的任务线程已随进程消失，但 jobs.json 里 status 仍停在
    running，会导致前端按钮永久置灰（点「识别字幕」无响应）。这里把孤儿任务回退到可继续状态。"""
    changed = False
    for jid, j in jobs.items():
        # 进程重启后线程已不存在；无论任务状态是否已被线程写回，都必须清掉残留 _task，
        # 否则后续操作会被误判为“已有后台操作正在运行”。
        if j.get("_task"):
            j["_task"] = None
            changed = True
        if j.get("status") == "running":
            if j.get("km_segments"):
                j["status"] = "dubbed"
            elif j.get("refined_segments"):
                j["status"] = "refined"
            elif j.get("cn_segments"):
                j["status"] = "asr_done"
            else:
                j["status"] = "uploaded"
            j["error"] = ""
            changed = True
    if changed:
        _save_jobs()


_load_jobs()
_recover_jobs()

app = FastAPI(title="中文短剧多语种本地化工作台")
# 所有工作台路由统一收在 /d 前缀下；文件服务在 /d/files
router = APIRouter(prefix="/d")
app.mount("/d/files", StaticFiles(directory=str(JOBS_DIR)), name="files")
# 兼容改 /d 前缀前建的老任务（其 source_file/draft 存的是 /files/... 旧路径）
app.mount("/files", StaticFiles(directory=str(JOBS_DIR)), name="files_legacy")
# 字体文件（用户自加字体丢进 fonts/ 即出现在下拉框；manifest.json 提供友好名字映射）
app.mount("/d/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")
def _session_token():
    if not WORKSTATION_API_KEY:
        return ""
    return hmac.new(WORKSTATION_API_KEY.encode(), b"cutly-session-v1", hashlib.sha256).hexdigest()


def _is_protected_path(path: str):
    return (path.startswith("/d/api/") and path not in {
        "/d/api/version", "/d/api/auth/login", "/d/api/auth/status"
    }) or path.startswith("/d/files/") or path.startswith("/files/") or path in {
        "/docs", "/redoc", "/openapi.json"
    }


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """用 HttpOnly 会话 Cookie 保护任务、素材与接口；登录页和版本接口保持可达。"""
    if _is_protected_path(request.url.path):
        if not WORKSTATION_API_KEY:
            return JSONResponse({"detail": "服务未配置 WORKSTATION_API_KEY"}, status_code=503)
        supplied = request.cookies.get(AUTH_COOKIE, "")
        if not hmac.compare_digest(supplied, _session_token()):
            return JSONResponse({"detail": "需要登录"}, status_code=401)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'",
    )
    if request.url.path.startswith("/d/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/d/")


@app.get("/api/version", include_in_schema=False)
def api_version():
    return {"version": APP_VER}


@router.get("/api/version", include_in_schema=False)
def api_version_d():
    return {"version": APP_VER}


@router.post("/api/client-log", include_in_schema=False)
def client_log(item: ClientLog):
    """接收前端操作诊断日志；严格截断字段，绝不记录访问密钥或字幕正文。"""
    detail = " ".join(item.detail.replace("\n", " ").split())
    suffix = f" jid={item.jid}" if item.jid else ""
    suffix += f" tid={item.tid}" if item.tid else ""
    suffix += f" client_at={item.client_at}" if item.client_at else ""
    pipeline.log(f"[client] {item.event[:80]}{suffix} {detail[:500]}")
    return {"ok": True}


@router.post("/api/auth/login", include_in_schema=False)
async def auth_login(request: Request):
    if not WORKSTATION_API_KEY:
        raise HTTPException(503, "服务未配置 WORKSTATION_API_KEY")
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}
    supplied = str(body.get("key", ""))
    if not hmac.compare_digest(supplied, WORKSTATION_API_KEY):
        await asyncio.sleep(0.5)
        raise HTTPException(401, "访问密钥错误")
    response = JSONResponse({"ok": True})
    response.set_cookie(AUTH_COOKIE, _session_token(), httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, max_age=12 * 60 * 60, path="/")
    return response


@router.get("/api/auth/status", include_in_schema=False)
def auth_status(request: Request):
    authenticated = bool(WORKSTATION_API_KEY) and hmac.compare_digest(
        request.cookies.get(AUTH_COOKIE, ""), _session_token())
    return {"configured": bool(WORKSTATION_API_KEY), "authenticated": authenticated}


@router.get("/api/fonts", include_in_schema=False)
def list_fonts():
    """扫描 fonts/ 目录，返回可用字体清单 [{family, url}]。
    - 若存在 manifest.json（预置字体带友好名字），用其映射；否则用文件名 stem。
    - 用户把 .ttf/.otf/.woff(2) 丢进 fonts/ 刷新即出现，无需改代码。"""
    manifest = {}
    mpath = FONTS_DIR / "manifest.json"
    if mpath.exists():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8")) or {}
        except Exception:
            manifest = {}
    out = []
    for f in sorted(FONTS_DIR.iterdir()):
        if f.suffix.lower() in FONT_EXTS:
            fam = manifest.get(f.name, f.stem)
            out.append({"family": fam, "url": f"/d/fonts/{f.name}"})
    if not out:
        out.append({"family": "系统默认", "url": ""})   # 空 url = 用浏览器/CSS 默认字体
    return out


# ---------- 数据模型 ----------
class JobCreate(BaseModel):
    video: str = ""


class SegEdit(BaseModel):
    index: int
    start: float
    end: float
    zh: str = Field(max_length=10000)
    hidden: bool = False
    style: dict | None = None   # 逐行字幕样式（字体/颜色/背景/加粗/位置），缺省用全局 subtitle_style


class SegsUpdate(BaseModel):
    segments: list[SegEdit]
    km_segments: list = Field(default_factory=list)   # 合并相邻句时同步；按 index 与 segments 对齐


class ConfigUpdate(BaseModel):
    target_lang: str = ""
    default_gender: str = ""


class ClientLog(BaseModel):
    event: str = Field(default="client", max_length=80)
    detail: str = Field(default="", max_length=500)
    jid: str = Field(default="", max_length=64)
    tid: str = Field(default="", max_length=64)
    client_at: str = Field(default="", max_length=40)


class ProjectsDeleteRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)


class ProjectSubtitleSelection(BaseModel):
    tid: str = Field(min_length=1, max_length=64)


class ProjectBatchResult(BaseModel):
    action: Literal["quick", "translate", "synthesize", "export"]
    state: Literal["success", "failed"]
    message: str = Field(default="", max_length=500)


class ClipsUpdate(BaseModel):
    video_clips: list = Field(default_factory=list)
    dub_hidden: bool = False


class AudioParamsUpdate(BaseModel):
    # 音频（单轨精简）：基础音源音量、配音音量、整体淡入/淡出（秒，0=不淡变）
    base_volume: float = Field(default=1.0, ge=0.0, le=4.0)
    dub_volume: float = Field(default=1.0, ge=0.0, le=4.0)
    fade_in: float = Field(default=0.0, ge=0.0, le=60.0)
    fade_out: float = Field(default=0.0, ge=0.0, le=60.0)


class AudioFullUpdate(BaseModel):
    # 撤销/重做回退用：音频整体状态一次性保存（禁音/音源/音量/淡入淡出）
    audio_muted: bool = False
    audio_source: str = "video"
    base_volume: float = 1.0
    dub_volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0


class SubShiftUpdate(BaseModel):
    # 字幕整体时间偏移（秒，正=推迟，负=提前）
    shift: float = Field(default=0.0, ge=-86400.0, le=86400.0)


class SubStyleUpdate(BaseModel):
    # 字幕样式：字号/颜色/背景/加粗/位置（上中下预设 + 拖动 x,y 比例）/字体族
    font_size: int = Field(default=28, ge=8, le=200)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    bg: bool = False
    bg_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    bg_opacity: float = Field(default=0.55, ge=0.0, le=1.0)
    bold: bool = False
    outline: bool = False
    position: Literal["bottom", "middle", "top", "custom"] = "bottom"
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.85, ge=0.0, le=1.0)
    font: str = Field(default="", max_length=120)
    box_width: float = Field(default=0.92, ge=0.1, le=1.0)
    box_height: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------- 多轨时间轴模型（v20260827009 起） ----------
# 一条轨道（track）可以是：video（视频图层）/ audio（音频轨）/ subtitle（字幕轨）/ image（图片叠加层）
# 列表顺序即图层 z 序：靠后的轨道在上层（视频/图片叠加时顶层覆盖下层）。
class TrackClip(BaseModel):
    model_config = ConfigDict(extra="allow")
    start: float = 0.0
    end: float = 0.0
    hidden: bool = False
    rotate: int = 0
    flip_h: bool = False
    flip_v: bool = False
    crop: dict | str | None = "none"
    speed: float = 1.0
    x: float = 0.5          # 叠加层水平中心比例 0~1
    y: float = 0.5          # 叠加层垂直中心比例 0~1
    scale: float = 1.0      # 叠加层缩放（1=满屏）
    opacity: float = 1.0    # 叠加层不透明度 0~1（图片轨用）


class TrackCue(BaseModel):
    model_config = ConfigDict(extra="allow")
    start: float = 0.0
    end: float = 0.0
    text: str = Field(default="", max_length=10000)
    hidden: bool = False
    style: dict | None = None
    voice: str = ""           # 隐藏声线标识，不显示在字幕文本中


class TrackModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["video", "audio", "subtitle", "image"]
    name: str = Field(default="", max_length=240)
    hidden: bool = False
    muted: bool = False
    src: str = Field(default="", max_length=1000)
    lang: str = "zh"        # subtitle/audio 配音语言
    role: str = ""          # subtitle: source(原始字幕) | translate(翻译字幕) | import(导入)
    clips: list[TrackClip] = Field(default_factory=list)
    cues: list[TrackCue] = Field(default_factory=list)
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    dubbed: bool = False
    dub_src: str = ""
    dub_lang: str = ""


class TracksUpdate(BaseModel):
    tracks: list[TrackModel] = Field(default_factory=list)


class AddTrackReq(BaseModel):
    media_id: str = ""      # 从媒体库把这条媒体添加为时间轴轨道


# ---------- 工具 ----------
def _set(jid, **kw):
    with LOCK:
        task_id = getattr(TASK_CONTEXT, "task_id", None)
        task = (jobs.get(jid) or {}).get("_task") or {}
        if task_id and (task.get("id") != task_id or task.get("aborted")):
            return False
        jobs[jid].update(kw)
        _save_jobs()
        return True


def _begin_task_locked(job, kind, tid=None):
    current = job.get("_task") or {}
    if current.get("id"):
        raise HTTPException(409, "该任务已有后台操作正在运行")
    task_id = uuid.uuid4().hex
    job["_task"] = {"id": task_id, "tid": tid, "kind": kind, "aborted": False,
                    "progress": 0, "message": "正在准备…", "prev_status": job.get("status"),
                    "started_at": time.time(), "last_log_progress": -1, "last_log_message": ""}
    job["status"] = "running"
    _save_jobs()
    pipeline.log(f"[job {job.get('id', '')}] task_start kind={kind} tid={tid or '-'} task={task_id}")
    return task_id


def _set_task_progress(jid, progress, message):
    """写入可轮询的任务进度。仅当前线程所属任务可更新，避免已中止任务覆盖新任务。"""
    timeline = None
    with LOCK:
        job = jobs.get(jid)
        task = (job or {}).get("_task") or {}
        task_id = getattr(TASK_CONTEXT, "task_id", None)
        if not job or not task_id or task.get("id") != task_id or task.get("aborted"):
            return False
        # 一键制作由三个既有后台步骤串行组成；每个子步骤仍可报告自己的 0~100，
        # 这里将它映射为总进度，前端始终看到单调递增的一条进度条。
        lo = float(task.get("progress_from", 0))
        hi = float(task.get("progress_to", 100))
        task["progress"] = max(0, min(100, int(lo + (hi - lo) * max(0, min(100, progress)) / 100)))
        task["message"] = str(message)
        if task["progress"] != task.get("last_log_progress") or task["message"] != task.get("last_log_message"):
            task["last_log_progress"] = task["progress"]
            task["last_log_message"] = task["message"]
            timeline = (task.get("kind", "task"), task.get("tid") or "-", task["progress"], task["message"])
        _save_jobs()
    if timeline:
        kind, tid, pct, text = timeline
        pipeline.log(f"[job {jid}] task_progress kind={kind} tid={tid} progress={pct}% message={text}")
    return True


def _run_thread(fn, jid, task_id, *a):
    TASK_CONTEXT.task_id = task_id
    started = time.monotonic()
    task_kind = "task"
    try:
        with LOCK:
            task_kind = ((jobs.get(jid) or {}).get("_task") or {}).get("kind", "task")
        fn(jid, *a)
    except Exception as ex:
        with LOCK:
            job = jobs.get(jid)
            task = (job or {}).get("_task") or {}
            if job and task.get("id") == task_id and not task.get("aborted"):
                job.update(status="error", error=str(ex))
                _save_jobs()
        pipeline.log(f"[job {jid}] 失败: {ex}")
    finally:
        with LOCK:
            job = jobs.get(jid)
            task = (job or {}).get("_task") or {}
            if job and task.get("id") == task_id:
                if job.get("status") == "running":
                    job["status"] = task.get("prev_status") or "uploaded"
                job["_task"] = None
                _save_jobs()
        pipeline.log(f"[job {jid}] task_end kind={task_kind} task={task_id} elapsed={time.monotonic() - started:.3f}s")
        TASK_CONTEXT.task_id = None


# ---------- 各步骤实现 ----------
def _step_asr(jid, model):
    jd = JOBS_DIR / jid
    jd.mkdir(parents=True, exist_ok=True)
    src = jobs[jid]["source"]
    _set_task_progress(jid, 3, "正在加载语音识别模型…")
    duration = float(jobs[jid].get("duration") or 0)
    def _asr_progress(position):
        ratio = min(1.0, position / duration) if duration > 0 else 0.0
        _set_task_progress(jid, 8 + int(ratio * 84), "正在识别字幕…")
    cn = pipeline.asr(src, model_size=model, progress_cb=_asr_progress)
    if not cn:
        raise RuntimeError("ASR 未识别到任何台词")
    segs = [{"index": i, "start": s, "end": e, "zh": t}
            for i, (s, e, t) in enumerate(cn)]
    pipeline.write_srt([(s, e, t) for s, e, t in cn], jd / "zh.srt")
    _set(jid, status="asr_done", cn_segments=segs,
         source_file=f"/d/files/{jid}/source.mp4")


def _step_dub(jid, lang):
    jd = JOBS_DIR / jid
    with LOCK:
        refined = jobs[jid].get("refined_segments") or jobs[jid].get("cn_segments") or []
        target_lang = lang or jobs[jid].get("target_lang", TARGET_LANG)
        default_gender = jobs[jid].get("default_gender", DEFAULT_GENDER)
    if not refined:
        raise RuntimeError("尚无字幕可配音，请先识别并精修")
    voice_map = VOICE_MAP.get(target_lang, VOICE_MAP[TARGET_LANG])
    # 解析性别 + 去标记文本
    parsed = []
    for s in refined:
        gender, clean = parse_gender(s.get("zh", ""), default_gender)
        parsed.append({"start": s["start"], "end": s["end"], "zh": s["zh"],
                       "clean": clean, "gender": gender})
    # 翻译（批量，行数不匹配回退逐句）
    clean_lines = [p["clean"] for p in parsed]
    _set_task_progress(jid, 5, "正在翻译字幕…")
    km_lines = pipeline.translate_lines(clean_lines, "", LANG_NAME.get(target_lang, target_lang),
                                        progress_cb=lambda p: _set_task_progress(jid, 5 + int(p * .4), "正在翻译字幕…"))
    if len(km_lines) != len(clean_lines):
        raise RuntimeError(f"翻译结果行数不匹配：输入 {len(clean_lines)} 行，输出 {len(km_lines)} 行")
    # 逐句合成（按性别选 voice）+ strict 时长对齐
    (jd / "tts").mkdir(exist_ok=True)
    tts_tmp = jd / "tts"
    wavs = []
    n_sped = 0
    for i, p in enumerate(parsed):
        _set_task_progress(jid, 45 + int((i + 1) * 45 / max(1, len(parsed))), f"正在生成配音 {i + 1}/{len(parsed)}…")
        voice, rate, pitch = tts_voice_profile(target_lang, p["gender"])
        mp3 = tts_tmp / f"{i:05d}.mp3"
        if not (mp3.exists() and mp3.stat().st_size > 100):
            # 关键：高棉语神经语音只接受高棉语文字，必须合成翻译后的 km 文本
            pipeline.synth_edge(km_lines[i], voice, str(mp3), rate=rate, pitch=pitch)
        wav = tts_tmp / f"{i:05d}.fit.wav"
        _, sped = pipeline._fit_clip(str(mp3), str(wav), max(p["end"] - p["start"], 0.0))
        if sped:
            n_sped += 1
        wavs.append((p["start"], p["end"], str(wav)))
        if (i + 1) % 20 == 0 or (i + 1) == len(parsed):
            pipeline.log(f"TTS {i + 1}/{len(parsed)}")
    duration = max(p["end"] for p in parsed)
    dub = pipeline.build_dub_audio(wavs, duration, str(tts_tmp))
    # 字幕 + 预览成片
    km_segs = [{"index": i, "start": p["start"], "end": p["end"], "zh": p["zh"],
                "km": km_lines[i], "gender": p["gender"]}
               for i, p in enumerate(parsed)]
    pipeline.write_srt([(s["start"], s["end"], s["km"]) for s in km_segs], jd / "km.srt")
    pipeline.mux(jobs[jid]["source"], dub, str(jd / "km.srt"), str(jd / "draft.mp4"), burn=False)
    _set(jid, status="dubbed", target_lang=target_lang, km_segments=km_segs,
         draft=f"/d/files/{jid}/draft.mp4", km_srt=f"/d/files/{jid}/km.srt",
         dub_wav=str(dub))
    pipeline.log(f"配音完成 {len(km_segs)} 句，{n_sped} 句加速对齐")


def _step_remove_vocals(jid):
    jd = JOBS_DIR / jid
    src = jobs[jid]["source"]
    audio_wav = jd / "orig_audio.wav"
    _set_task_progress(jid, 5, "正在准备音频…")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "44100", str(audio_wav)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    out_dir = jd / "demucs"
    try:
        subprocess.run(
            [sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
             "-o", str(out_dir), str(audio_wav)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    except FileNotFoundError:
        raise RuntimeError("demucs 未安装，请先 pip install demucs")
    # demucs 输出: <out>/htdemucs/<name>/no_vocals.wav
    name = audio_wav.stem
    nv = out_dir / "htdemucs" / name / "no_vocals.wav"
    if not nv.exists():
        raise RuntimeError(f"demucs 未产出 no_vocals: {nv}")
    shutil.copy(nv, jd / "instr.wav")
    # 还原到去人声前的步骤状态（避免卡在 running）
    with LOCK:
        job = jobs[jid]
        restored = "dubbed" if job.get("km_segments") else ("refined" if job.get("refined_segments") else "asr_done")
    # 去人声后基础音源切换为伴奏（instrumental）；导出时与配音混流
    _set(jid, vocals_removed=True, instr_wav=str(jd / "instr.wav"),
         audio_source="instrumental", status=restored)


def _step_extract_audio(jid):
    """从视频分离原声（抽音轨），作为可编辑的基础音源 audio_source='video'。"""
    jd = JOBS_DIR / jid
    src = jobs[jid]["source"]
    out = jd / "orig_audio.wav"
    _set_task_progress(jid, 5, "正在分离音频…")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "44100", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    with LOCK:
        job = jobs[jid]
        restored = "dubbed" if job.get("km_segments") else ("refined" if job.get("refined_segments") else "asr_done")
    _set(jid, orig_audio=str(out), audio_source="video", status=restored)


def _resolve_base_audio(job, jd):
    """按 audio_source 解析导出的基础音源文件路径（不存在则现抽）。"""
    src = job["source"]
    a_src = job.get("audio_source", "video")
    instr = job.get("instr_wav")
    added = job.get("added_audio")
    orig = job.get("orig_audio")
    if a_src == "instrumental" and instr and Path(instr).exists():
        return str(instr)
    if a_src == "added" and added and Path(added).exists():
        return str(added)
    # video：复用已分离的原声；否则从源视频现抽
    if orig and Path(orig).exists():
        return str(orig)
    oa = jd / "orig_export.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "44100", str(oa)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    return str(oa)


def _probe_res(path):
    """返回 (width, height)，失败回退 1920x1080。"""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        parts = (pr.stdout or "").strip().split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1920, 1080


def _needs_trim(vclips, dur):
    """判断视频片段是否真的需要重新裁剪/拼接（未编辑则走原整段导出，零风险）。"""
    if not vclips or len(vclips) != 1:
        return bool(vclips)
    c = vclips[0]
    if c.get("hidden"):
        return True
    s, e = c.get("start", 0), c.get("end", dur)
    if abs(s) > 0.3 or (dur and abs(e - dur) > 0.3):
        return True
    # 任一变换（旋转/翻转/裁剪/变速）即需重新处理
    if int(c.get("rotate", 0) or 0) % 360:
        return True
    if c.get("flip_h") or c.get("flip_v"):
        return True
    if c.get("crop"):
        return True
    if abs(float(c.get("speed", 1.0) or 1.0) - 1.0) > 0.01:
        return True
    return False


def _crop_filter(crop, W, H):
    """根据裁剪预设返回 crop 过滤器串（在源像素坐标下）。"""
    if isinstance(crop, str):
        mode = crop or "none"
    elif isinstance(crop, dict):
        mode = crop.get("mode", "none") or "none"
    else:
        mode = "none"
    if mode in ("none", "", "原始", "自由"):
        return ""
    if mode == "1:1":
        side = min(W, H)
        x = (W - side) // 2
        y = (H - side) // 2
        return f",crop={side}:{side}:{x}:{y}"
    if mode in ("9:16", "9-16"):
        tw = int(H * 9 / 16)
        if tw > W:
            tw = W
        x = (W - tw) // 2
        return f",crop={tw}:{H}:{x}:0"
    if mode in ("4:5", "4-5"):
        tw = int(H * 4 / 5)
        if tw > W:
            tw = W
        x = (W - tw) // 2
        return f",crop={tw}:{H}:{x}:0"
    if mode in ("1:1?free",):
        return ""
    return ""


def _trim_video(src, vclips, out):
    """按可见片段裁剪+拼接视频（含音轨），并应用每片段变换：
    变速(setpts/atempo) / 旋转(transpose) / 翻转(hflip,vflip) / 裁剪(crop)。
    每段统一 scale+pad 回源分辨率，避免 concat 因分辨率/奇偶不一致失败。"""
    vis = [c for c in vclips if not c.get("hidden")]
    if not vis:
        raise RuntimeError("所有视频片段均被隐藏")
    W, H = _probe_res(src)
    n = len(vis)
    vf, af = [], []
    for i, c in enumerate(vis):
        s = max(0.0, float(c.get("start", 0)))
        e = max(s + 0.1, float(c.get("end", 0)))
        sp = float(c.get("speed", 1.0) or 1.0)
        # 视频链
        vchain = f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS"
        if abs(sp - 1.0) > 0.01:
            vchain += f",setpts=PTS/{sp:.3f}"
        rot = int(c.get("rotate", 0) or 0) % 360
        if rot == 90:
            vchain += ",transpose=1"
        elif rot == 270:
            vchain += ",transpose=2"
        elif rot == 180:
            vchain += ",transpose=1,transpose=1"
        if c.get("flip_h"):
            vchain += ",hflip"
        if c.get("flip_v"):
            vchain += ",vflip"
        vchain += _crop_filter(c.get("crop"), W, H)
        # 归一化到源分辨率（保持比例 + 居中填充），保证 concat 输入一致
        vchain += f",scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[v{i}]"
        vf.append(vchain)
        # 音频链
        achain = f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS"
        if abs(sp - 1.0) > 0.01:
            achain += f",atempo={sp:.3f}"
        achain += f"[a{i}]"
        af.append(achain)
    vconcat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[ov]"
    aconcat = "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[oa]"
    fc = ";".join(vf + af + [vconcat, aconcat])
    subprocess.run(["ffmpeg", "-y", "-i", src, "-filter_complex", fc,
                   "-map", "[ov]", "-map", "[oa]", "-c:v", "libx264", "-c:a", "aac",
                   "-preset", "veryfast", str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    return str(out)


# ---------- 多轨工具 ----------
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")
SUB_EXTS = (".srt", ".ass", ".vtt")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def _media_kind(filename: str):
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in SUB_EXTS:
        return "subtitle"
    if ext in IMG_EXTS:
        return "image"
    raise HTTPException(400, f"不支持的文件类型: {ext or '无扩展名'}")


def _safe_filename(filename: str):
    """UploadFile.filename 由客户端提供，只保留安全文件名，禁止目录和控制字符。"""
    name = (filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    name = re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE)
    return name[:180] or "upload"


def _save_upload(file: UploadFile, dest: Path):
    root = JOBS_DIR.resolve()
    resolved = dest.resolve()
    if not resolved.is_relative_to(root):
        raise HTTPException(400, "非法保存路径")
    written = 0
    try:
        with open(resolved, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制")
                out.write(chunk)
    except OSError as exc:
        resolved.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise HTTPException(507, "服务器磁盘空间不足，请清理旧项目后重试") from exc
        raise
    except Exception:
        resolved.unlink(missing_ok=True)
        raise


def _new_track_id(job):
    n = len(job.get("tracks") or [])
    return f"t{n+1}_{uuid.uuid4().hex[:4]}"


def _files_url(jid, name):
    return f"/d/files/{jid}/{name}"


def _local_of(jid, url):
    """把 /d/files/{jid}/xxx 形式的 URL 转回本机路径。"""
    if not url:
        return ""
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
        return ""
    m = re.search(r"/(?:d/)?files/[^/]+/(.+)$", url)
    if m:
        relative = m.group(1)
    elif url.startswith("/"):
        return ""
    else:
        relative = url
    candidate = (JOBS_DIR / jid / relative).resolve()
    job_root = (JOBS_DIR / jid).resolve()
    return str(candidate) if candidate.is_relative_to(job_root) else ""


def _has_audio(local):
    """用 ffprobe 判断文件是否含音轨。"""
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                             "-show_entries", "stream=index", "-of", "csv=p=0", str(local)],
                            capture_output=True, text=True, timeout=20)
        return bool((pr.stdout or "").strip())
    except Exception:
        return False


def _parse_subtitle_file(path):
    """解析 .srt/.vtt/.ass 为 cues 列表（[{start,end,text}]）。"""
    p = Path(path)
    if not p.exists():
        return []
    txt = p.read_text(encoding="utf-8", errors="ignore")
    cues = []
    if p.suffix.lower() == ".ass":
        for line in txt.splitlines():
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) >= 10:
                    start, end, text = parts[1].strip(), parts[2].strip(), parts[9].strip()
                    cues.append({"start": _tc(start), "end": _tc(end), "text": text})
    elif p.suffix.lower() == ".vtt":
        blocks = re.split(r"\n\s*\n", txt)
        for b in blocks:
            lines = [line for line in b.splitlines() if line.strip() and not line.strip().startswith("WEBVTT")]
            if not lines:
                continue
            mm = re.search(r"(\d+:\d+:\d+\.\d+)\s*-->\s*(\d+:\d+:\d+\.\d+)", lines[0])
            if mm:
                cues.append({"start": _tc(mm.group(1)), "end": _tc(mm.group(2)),
                             "text": " ".join(lines[1:])})
    else:  # srt
        blocks = re.split(r"\n\s*\n", txt)
        for b in blocks:
            lines = [line for line in b.splitlines() if line.strip()]
            if not lines:
                continue
            mm = re.search(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)", lines[0])
            if mm:
                cues.append({"start": _tc(mm.group(1)), "end": _tc(mm.group(2)),
                             "text": " ".join(lines[1:])})
    return cues


def _tc(s):
    """把 00:00:00.000 / 00:00.000 时间码转秒。"""
    s = (s or "").strip().replace(",", ".")
    parts = s.split(":")
    try:
        parts = [float(x) for x in parts]
    except Exception:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def ensure_tracks(job):
    """为兼容旧任务：若没有 tracks，则根据旧字段重建一套等价的多轨。
    新上传的任务在 upload 时已直接写好 tracks（可能为合法空数组 []），不再走这里。
    注意：守卫必须用 is not None，不能用 truthy——空数组 [] 表示「已初始化、无轨道」，
    若用 falsy 判断会让每次 GET 都把已删除的轨道重建回来（删除失效/删视频冒音频）。"""
    if job.get("tracks") is not None:
        return job["tracks"]
    tracks = []
    dur = job.get("duration") or 0.0
    src_url = job.get("source_file") or (f"/d/files/{job['id']}/source.mp4" if job.get("source") else "")
    # 视频轨（底层）
    vclips = job.get("video_clips") or []
    if job.get("source"):
        tracks.append({
            "id": "t_v", "kind": "video", "name": "视频", "hidden": False, "muted": False,
            "src": src_url, "lang": "zh", "role": "",
            "clips": vclips or [{"start": 0.0, "end": dur, "hidden": False}],
            "cues": [], "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
            "dubbed": False, "dub_src": "", "dub_lang": "",
        })
    # 字幕轨：source(原始)=refined/cn；translate(翻译)=km
    refined = job.get("refined_segments") or job.get("cn_segments") or []
    if refined:
        tracks.append({
            "id": "t_sub_src", "kind": "subtitle", "name": "字幕", "hidden": False,
            "muted": False, "src": "", "lang": "zh", "role": "source",
            "clips": [], "cues": [{"start": s.get("start", 0), "end": s.get("end", 0),
                                   "text": s.get("zh", ""), "hidden": bool(s.get("hidden")),
                                   "style": s.get("style")} for s in refined],
            "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0, "dubbed": False, "dub_src": "", "dub_lang": "",
        })
    km = job.get("km_segments") or []
    if km:
        tracks.append({
            "id": "t_sub_tr", "kind": "subtitle", "name": "翻译字幕", "hidden": job.get("dub_hidden", False),
            "muted": False, "src": "", "lang": job.get("target_lang", TARGET_LANG), "role": "translate",
            "clips": [], "cues": [{"start": s.get("start", 0), "end": s.get("end", 0),
                                   "text": s.get("km", ""), "hidden": False, "style": None} for s in km],
            "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0, "dubbed": False, "dub_src": "", "dub_lang": "",
        })
    # 音频轨：旧模型的基础音源（原声/伴奏/添加音频）+ 已生成的配音
    a_src = job.get("audio_source", "video")
    base_url = ""
    if a_src == "added" and job.get("added_audio"):
        base_url = _files_url(job["id"], Path(job["added_audio"]).name)
    elif job.get("orig_audio"):
        base_url = _files_url(job["id"], Path(job["orig_audio"]).name)
    elif job.get("source"):
        base_url = src_url
    if base_url or a_src != "none":
        tracks.append({
            "id": "t_aud", "kind": "audio", "name": "音频", "hidden": False,
            "muted": bool(job.get("audio_muted", False)), "src": base_url, "lang": "zh", "role": "",
            "clips": [{"start": 0.0, "end": dur, "hidden": False}],
            "cues": [], "volume": float(job.get("base_volume", 1.0) or 1.0),
            "fade_in": float(job.get("fade_in", 0) or 0), "fade_out": float(job.get("fade_out", 0) or 0),
            "dubbed": bool(job.get("dub_wav") and Path(job["dub_wav"]).exists()),
            "dub_src": _files_url(job["id"], Path(job["dub_wav"]).name) if job.get("dub_wav") else "",
            "dub_lang": job.get("target_lang", TARGET_LANG),
        })
    job["tracks"] = tracks
    # 兼容旧任务：媒体库由 tracks 反推（旧任务没有独立 media 列表）
    if not job.get("media"):
        job["media"] = [{
            "id": t.get("id"), "name": t.get("name"), "kind": t.get("kind"),
            "src": t.get("src"), "lang": t.get("lang", ""), "role": t.get("role", ""),
            "dur": (t.get("clips") or [{}])[0].get("end", 0) if t.get("kind") in ("video", "audio", "image") else 0,
            "clips": t.get("clips", []), "cues": t.get("cues", []),
        } for t in tracks]
    return tracks


def _normalize(job):
    """get_job 返回前统一补 tracks，保证前端始终拿到多轨结构。
    仅当 tracks 为 None（老任务缺失该键）才重建；空数组 [] 是合法的「无轨道」状态，绝不重建。"""
    if job.get("tracks") is None:
        ensure_tracks(job)
    return job


def _step_export(jid):
    """统一导出入口：只允许所见即所得的多轨合成；失败必须明确报错，绝不伪装成功。"""
    with LOCK:
        ensure_tracks(jobs[jid])
    _export_multitrack(jid)
    # 视频成功后再生成文案；文案失败不影响已完成的视频合成和下载。
    with LOCK:
        job = jobs[jid]
        source_track = next((t for t in (job.get("tracks") or [])
                             if t.get("kind") == "subtitle" and t.get("role") == "source"), None)
        cn_lines = [re.sub(r"^\s*\[[^\]]+\]\s*", "", str(c.get("text") or "")).strip()
                    for c in ((source_track or {}).get("cues") or []) if not c.get("hidden")]
        if not cn_lines:
            cn_lines = [str(s.get("zh") or "").strip() for s in
                        (job.get("refined_segments") or job.get("cn_segments") or [])]
        target_lang = job.get("target_lang", TARGET_LANG)
        target_name = LANG_NAME.get(target_lang, target_lang)
    _set_task_progress(jid, 96, "视频合成完成，正在生成三语文案…")
    try:
        promo = pipeline.generate_promo_copy(cn_lines, target_name)
        promo["target_code"] = target_lang
        promo["generated_at"] = time.time()
        with LOCK:
            job = jobs[jid]
            job["promo_copy"] = promo
            job.pop("promo_error", None)
            # 已保存项目同步更新 manifest/index，项目库无需另存即可拿到新文案。
            if jid in _read_projects_index()["projects"]:
                _save_project_locked(job)
            _save_jobs()
        pipeline.log(f"[job {jid}] promo_copy_generated target={target_lang}")
    except Exception as ex:
        with LOCK:
            jobs[jid]["promo_error"] = str(ex)[:500]
            _save_jobs()
        pipeline.log(f"[job {jid}] promo_copy_failed: {ex}")
    _set_task_progress(jid, 100, "视频合成完成")


def _export_legacy(jid):
    """旧模型导出（单视频 + 单音源 + 单字幕/配音），兼容存量任务。"""
    jd = JOBS_DIR / jid
    job = jobs[jid]
    dub = job.get("dub_wav")
    src = job.get("source")
    dur = job.get("duration") or 0.0
    vclips = job.get("video_clips") or []
    dub_hidden = job.get("dub_hidden", False)
    refined = job.get("refined_segments") or job.get("cn_segments") or []
    km_segs = job.get("km_segments") or []

    # 导出文件名 = 保存的项目名称（title），非法字符清洗；默认“未命名项目”
    raw_title = (job.get("title") or "").strip() or "未命名项目"
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw_title).strip().strip(".")[:180] or "未命名项目"
    final_name = f"{safe_title}.mp4"

    orig_srt = jd / "km.srt"
    if km_segs:
        if any(s.get("hidden") for s in refined):
            kept_pairs = [(km_segs[i], i) for i in range(len(km_segs))
                          if i < len(refined) and not refined[i].get("hidden")]
        else:
            kept_pairs = [(km_segs[i], i) for i in range(len(km_segs))]
    else:
        kept_pairs = []
    keep_segs = [p[0] for p in kept_pairs]
    shift = float(job.get("sub_shift", 0) or 0)
    if shift and keep_segs:
        keep_segs = [dict(s, start=max(0.0, float(s["start"]) + shift),
                          end=max(0.0, float(s["end"]) + shift)) for s in keep_segs]
        kept_pairs = [(dict(p[0], start=keep_segs[i]["start"], end=keep_segs[i]["end"]), p[1])
                      for i, p in enumerate(kept_pairs)]
    if (shift and keep_segs) or (kept_pairs and any(refined[p[1]].get("hidden") for p in kept_pairs)):
        pipeline.write_srt([(s["start"], s["end"], s["km"]) for s in keep_segs],
                           jd / "km_export.srt")
        srt_path = jd / "km_export.srt"
    else:
        srt_path = orig_srt

    style = job.get("subtitle_style") or {}
    has_line_style = any((i < len(refined) and refined[i].get("style")) for _, i in kept_pairs)
    if style or has_line_style:
        W, H = _probe_res(src)
        ass_segs = [(s["start"], s["end"], s["km"]) for s, _ in kept_pairs]
        line_styles = [refined[i].get("style") if i < len(refined) else None
                       for _, i in kept_pairs]
        pipeline.write_ass(ass_segs, jd / "km.ass", style, W, H, styles=line_styles)
        burn_path = str(jd / "km.ass")
        do_burn = True
    else:
        burn_path = str(srt_path)
        do_burn = False

    audio_muted = bool(job.get("audio_muted", False))
    base_volume = max(0.0, min(4.0, float(job.get("base_volume", 1.0) or 1.0)))
    dub_volume = max(0.0, min(4.0, float(job.get("dub_volume", 1.0) or 1.0)))
    fade_in = max(0.0, float(job.get("fade_in", 0) or 0))
    fade_out = max(0.0, float(job.get("fade_out", 0) or 0))
    dub_ok = bool(dub) and Path(dub).exists()

    def _fade_suffix(fi, fo, d):
        s = ""
        if fi > 0:
            s += f",afade=t=in:st=0:d={fi:.3f}"
        if fo > 0:
            st = max(0.0, d - fo)
            s += f",afade=t=out:st={st:.3f}:d={fo:.3f}"
        return s

    if audio_muted:
        audio = None
    else:
        base = _resolve_base_audio(job, jd)
        fade = _fade_suffix(fade_in, fade_out, dur or 0)
        if (not dub_hidden) and dub_ok:
            mixed = jd / "mixed.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", base, "-i", dub,
                 "-filter_complex",
                 f"[0]volume={base_volume:.3f}[a];[1]volume={dub_volume:.3f}[b];"
                 f"[a][b]amix=inputs=2:duration=longest[m]{fade}",
                 "-map", "[m]", "-ar", "44100", "-ac", "1", str(mixed)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
            audio = str(mixed)
        else:
            if (not dub_hidden) and (not dub_ok):
                pipeline.log("配音尚未生成，导出将仅使用基础音源（原声）")
            proc = jd / "audio_base.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", base, "-af", f"[0]volume={base_volume:.3f}{fade}",
                 "-ar", "44100", "-ac", "1", str(proc)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
            audio = str(proc)

    if vclips and _needs_trim(vclips, dur):
        try:
            vsrc = _trim_video(src, vclips, jd / "trimmed.mp4")
        except Exception as ex:
            pipeline.log(f"视频裁剪失败，回退整段: {ex}")
            vsrc = src
    else:
        vsrc = src

    try:
        if do_burn:
            pipeline.mux(vsrc, audio, burn_path, str(jd / final_name), burn=True)
        else:
            pipeline.mux(vsrc, audio, burn_path, str(jd / final_name), burn=False)
    except Exception as ex:
        pipeline.log(f"增强导出失败，回退原始整段导出: {ex}")
        pipeline.mux(src, dub, str(orig_srt), str(jd / final_name), burn=False)

    _set(jid, status="exported", final=f"/d/files/{jid}/{final_name}")


def _export_multitrack_v1(jid):
    """多轨合成：视频轨按列表顺序底层→顶层 overlay（顶层覆盖下层，时间段不重叠处下层显示）；
    图片轨带透明度/位置叠加；音频轨全部非静音轨 amix；字幕轨全部 burn（合并进一个 ASS）。"""
    jd = JOBS_DIR / jid
    job = jobs[jid]
    tracks = job.get("tracks") or []
    raw_title = (job.get("title") or "").strip() or "未命名项目"
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw_title).strip().strip(".") or "未命名项目"
    final_name = f"{safe_title}.mp4"

    vids = [t for t in tracks if t["kind"] == "video" and not t["hidden"]]
    # 时间轴数组靠前为更上层；图片位于视频轨下方时应被不透明视频遮挡，不能再固定叠到所有视频之上。
    imgs = [t for pos, t in enumerate(tracks) if t["kind"] == "image" and not t["hidden"]
            and not any(upper.get("kind") == "video" and not upper.get("hidden") for upper in tracks[:pos])]
    # 音频轨（非静音）；「分离原声」产生的轨 role='separated'，与用户导入的音频轨一视同仁参与混音
    auds = [t for t in tracks if t["kind"] == "audio" and not t["muted"]]
    subs = [t for t in tracks if t["kind"] == "subtitle" and not t["hidden"]]
    if not vids:
        raise RuntimeError("无视频轨，无法多轨合成（请先添加视频）")
    dur = float(job.get("duration") or 0.0)
    for t in vids:
        for c in (t.get("clips") or [{}]):
            dur = max(dur, float(c.get("end", 0) or 0))

    # ---------- 视频图层合成 ----------
    # z 序约定：tracks 列表中「靠前 = 更靠上」。ffmpeg overlay 后者在上，故从列表末尾向前叠加，
    # 使第 0 条视频处于最上层（与 UI 拖拽顺序直觉一致：列表顶部轨覆盖在下面轨之上）。
    inputs = []
    fc = []
    vse = []   # 每条视频轨的 (start, end)，用于 overlay enable
    for i, t in enumerate(vids):
        local = _local_of(jid, t["src"])
        if not local or not Path(local).exists():
            raise RuntimeError(f"视频轨缺失文件: {t.get('name')}")
        inputs += ["-i", local]
        clip = (t.get("clips") or [{}])[0] or {}
        s = float(clip.get("start", 0) or 0)
        e = float(clip.get("end", dur) or dur) or dur
        vse.append((s, e))
        # clip 的 start/end 是时间轴坐标，不能把所有视频都重置到 0 秒。
        chain = f"[{i}:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS+{s:.3f}/TB"
        rot = int(clip.get("rotate", 0) or 0) % 360
        if rot == 90:
            chain += ",transpose=1"
        elif rot == 270:
            chain += ",transpose=2"
        elif rot == 180:
            chain += ",transpose=1,transpose=1"
        if clip.get("flip_h"):
            chain += ",hflip"
        if clip.get("flip_v"):
            chain += ",vflip"
        cw, ch = _probe_res(local)
        chain += _crop_filter(clip.get("crop"), cw, ch)
        chain += f"[v{i}]"
        fc.append(chain)
    n = len(vids)
    if n == 1:
        cur = "[v0]"
    else:
        # 从最底（列表最后一条）向上叠，最终第 0 条在最上层
        cur = f"[v{n-1}]"
        for i in range(n-2, -1, -1):
            s, e = vse[i]
            enable = f":enable='between(t,{s:.3f},{e:.3f})'"
            cur = f"{cur}[v{i}]overlay=0:0{enable}[ov{i}]"
        fc.append(cur)
    # 过滤图表达式已经写入 fc；map 只能引用最终输出标签，不能把整段表达式当作标签。
    final_v = "[v0]" if n == 1 else "[ov0]"
    cur = final_v
    # 图片叠加（透明层，始终在视频之上；图片之间同样靠前=靠上）
    for pos in range(len(imgs)-1, -1, -1):
        t = imgs[pos]
        local = _local_of(jid, t["src"])
        if not local or not Path(local).exists():
            continue
        inputs += ["-i", local]
        idx = len(vids) + pos
        clip = (t.get("clips") or [{}])[0] or {}
        s = float(clip.get("start", 0) or 0)
        e = float(clip.get("end", dur) or dur) or dur
        op = float(clip.get("opacity", 1.0) or 1.0)
        x = float(clip.get("x", 0.5) or 0.5)
        y = float(clip.get("y", 0.5) or 0.5)
        sc = float(clip.get("scale", 1.0) or 1.0)
        fc.append(f"[{idx}:v]loop=loop=-1:size=1:start=0,scale='trunc(iw*{sc:.3f}/2)*2':'-2',"
                  f"format=rgba,colorchannelmixer=aa={op:.3f}[im{idx}]")
        enable = f":enable='between(t,{s:.3f},{e:.3f})'"
        ox = f"(W-w)*{x:.3f}"
        oy = f"(H-h)*{y:.3f}"
        expr = f"{cur}[im{idx}]overlay={ox}:{oy}{enable}[ovim{idx}]"
        fc.append(expr)
        cur = f"[ovim{idx}]"
    final_v = cur  # 已是合法 label，如 [ovim3] 或 [ov0] 或 [v0]

    # ---------- 字幕（只烧录「一条」字幕轨：优先翻译轨=目标语言，否则源字幕轨，避免两种语言重叠） ----------
    burn_sub = (next((t for t in subs if t.get("role") == "translate"), None)
                or next((t for t in subs if t.get("role") == "source"), None)
                or (subs[0] if subs else None))
    all_cues = []
    if burn_sub:
        for c in (burn_sub.get("cues") or []):
            if c.get("hidden"):
                continue
            all_cues.append((float(c.get("start", 0) or 0), float(c.get("end", 0) or 0),
                             c.get("text", ""), c.get("style")))
    shift = float(job.get("sub_shift", 0) or 0)
    if shift:
        all_cues = [(max(0.0, a + shift), max(0.0, b + shift), txt, st)
                    for a, b, txt, st in all_cues]
    do_burn = bool(all_cues)
    if do_burn:
        W, H = _probe_res(_local_of(jid, vids[0]["src"]))
        pipeline.write_ass([(a, b, txt) for a, b, txt, st in all_cues], jd / "km.ass",
                           job.get("subtitle_style") or {}, W, H,
                           styles=[st for a, b, txt, st in all_cues])
        burn_path = str(jd / "km.ass")
    else:
        burn_path = ""

    # ---------- 音频混流 ----------
    # 音频来源：(1) 未执行「分离原声」的视频自带音轨 [i:a]；(2) 所有非静音音频轨（可含配音）。
    # 已分离的视频会标记 muted_src_audio=True，其自带音静音，避免与分离出的音轨叠加成双倍原声。
    afc = []
    mix_labels = []
    # (1) 视频自带音（未分离且确实有音轨）
    for i, t in enumerate(vids):
        if t.get("muted_src_audio"):
            continue
        local = _local_of(jid, t["src"])
        if not (local and Path(local).exists() and _has_audio(local)):
            continue
        s, e = vse[i]
        afc.append(f"[{i}:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,adelay={int(s*1000)}:all=1[a_v{i}]")
        mix_labels.append(f"[a_v{i}]")
    # (2) 音频轨
    for t in auds:
        local = _local_of(jid, (t["dubbed"] and t.get("dub_src")) or t["src"])
        if not local or not Path(local).exists():
            local = _local_of(jid, t["src"])
        if not local or not Path(local).exists():
            continue
        inputs += ["-i", local]
        ai = len(inputs) // 2 - 1
        vol = max(0.0, min(4.0, float(t.get("volume", 1.0) or 1.0)))
        fi = float(t.get("fade_in", 0) or 0)
        fo = float(t.get("fade_out", 0) or 0)
        fade = ""
        if fi > 0:
            fade += f",afade=t=in:st=0:d={fi:.3f}"
        if fo > 0:
            fade += f",afade=t=out:st={max(0.0, dur - fo):.3f}:d={fo:.3f}"
        afc.append(f"[{ai}:a]volume={vol:.3f}{fade}[a{ai}]")
        mix_labels.append(f"[a{ai}]")
    if not mix_labels:
        audio_out = None
    elif len(mix_labels) == 1:
        audio_out = mix_labels[0]
    else:
        afc.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=longest[aout]")
        audio_out = "[aout]"

    # ---------- 组装 ffmpeg ----------
    cmd = ["ffmpeg", "-y"] + inputs
    cmd += ["-filter_complex", ";".join(fc + afc)]
    cmd += ["-map", final_v]
    if audio_out:
        cmd += ["-map", audio_out, "-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast"]
    if do_burn and burn_path:
        cmd += ["-vf", f"subtitles={burn_path}"]
    cmd += [str(jd / final_name)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    _set(jid, status="exported", final=f"/d/files/{jid}/{final_name}")


def _merge_time_intervals(intervals):
    """合并视频可见区间，供删除区间后的最终紧凑输出使用。"""
    merged = []
    for start, end in sorted(intervals):
        start, end = max(0.0, float(start)), max(0.0, float(end))
        if end - start < 0.001:
            continue
        if merged and start <= merged[-1][1] + 0.002:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _visible_clips(track, fallback_end=0.0):
    clips = track.get("clips") or []
    if not clips and fallback_end > 0:
        clips = [{"start": 0.0, "end": fallback_end, "hidden": False}]
    result = []
    for clip in clips:
        if clip.get("hidden"):
            continue
        try:
            start = max(0.0, float(clip.get("start", 0) or 0))
            end = max(start, float(clip.get("end", fallback_end) or fallback_end))
        except (TypeError, ValueError):
            continue
        if end - start >= 0.01:
            result.append((clip, start, end))
    return result


def _compose_visible_subtitles(sub_tracks, shift, base_style):
    """把所有可见字幕轨按时间切片并合成换行文本，匹配前端同时显示多轨字幕的行为。"""
    prepared = []
    points = set()
    for track in sub_tracks:
        cues = []
        for cue in track.get("cues") or []:
            if cue.get("hidden") or not str(cue.get("text", "")).strip():
                continue
            start = max(0.0, float(cue.get("start", 0) or 0) + shift)
            end = max(start, float(cue.get("end", 0) or 0) + shift)
            if end - start < 0.01:
                continue
            text = parse_gender(str(cue.get("text", "")))[1]
            cues.append((start, end, text, cue.get("style")))
            points.update((start, end))
        prepared.append(cues)
    ordered = sorted(points)
    result = []
    for start, end in zip(ordered, ordered[1:]):
        if end - start < 0.005:
            continue
        mid = (start + end) / 2.0
        active = []
        for cues in prepared:
            cue = next((item for item in cues if item[0] <= mid < item[1]), None)
            if cue:
                active.append(cue)
        if not active:
            continue
        text = "\n".join(item[2] for item in active if item[2].strip())
        if not text:
            continue
        style = next((item[3] for item in active if item[3]), None) or base_style
        if result and result[-1][2] == text and result[-1][3] == style and abs(result[-1][1] - start) < 0.01:
            result[-1] = (result[-1][0], end, text, style)
        else:
            result.append((start, end, text, style))
    return result


def _export_multitrack(jid):
    """按统一时间轴数据生成成片：全部片段、图层、音频和可见字幕均进入同一 FFmpeg 图。"""
    jd = JOBS_DIR / jid
    with LOCK:
        job = copy.deepcopy(jobs[jid])
    tracks = job.get("tracks") or []
    raw_title = (job.get("title") or "").strip() or "未命名项目"
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw_title).strip().strip(".") or "未命名项目"
    final_name = f"{safe_title}.mp4"
    output_path = jd / final_name
    base_duration = max(0.0, float(job.get("duration") or 0.0))

    video_tracks = [t for t in tracks if t.get("kind") == "video" and not t.get("hidden")]
    if not video_tracks:
        raise RuntimeError("无可见视频轨，无法导出")
    video_intervals = []
    for track in video_tracks:
        video_intervals.extend((start, end) for _, start, end in _visible_clips(track, base_duration))
    active_intervals = _merge_time_intervals(video_intervals)
    if not active_intervals:
        raise RuntimeError("所有视频片段均已删除或隐藏，无法导出")

    # 画布取持续时间最长的视频轨分辨率，保证短上层视频不会改变项目基准尺寸。
    clock_track = max(video_tracks, key=lambda t: max((e for _, _, e in _visible_clips(t, base_duration)), default=0.0))
    clock_local = _local_of(jid, clock_track.get("src", ""))
    if not clock_local or not Path(clock_local).exists():
        raise RuntimeError("基准视频文件不存在")
    width, height = _probe_res(clock_local)
    width -= width % 2
    height -= height % 2

    duration = max(base_duration, max(end for _, end in active_intervals))
    for track in tracks:
        if track.get("hidden"):
            continue
        if track.get("kind") in ("image", "audio"):
            duration = max(duration, max((end for _, _, end in _visible_clips(track, 0.0)), default=0.0))
        elif track.get("kind") == "subtitle":
            duration = max(duration, max((float(c.get("end", 0) or 0) for c in track.get("cues") or []), default=0.0))
    duration = max(duration, 0.1)

    inputs = []
    input_count = 0
    filters = [f"color=c=black:s={width}x{height}:r=30:d={duration:.3f}[canvas]"]
    mix_labels = []

    def add_input(path, options=None):
        nonlocal input_count
        idx = input_count
        input_count += 1
        inputs.extend(list(options or []) + ["-i", str(path)])
        return idx

    def overlay(cur, layer, start, end, tag):
        out = f"[vis_{tag}]"
        filters.append(f"{cur}{layer}overlay=0:0:eof_action=pass:repeatlast=0:enable='between(t,{start:.3f},{end:.3f})'{out}")
        return out

    # 靠前轨道在上层，因此从数组末尾向前构建画面。
    current_video = "[canvas]"
    visual_counter = 0
    for track in reversed(tracks):
        kind = track.get("kind")
        if track.get("hidden") or kind not in ("video", "image"):
            continue
        local = _local_of(jid, track.get("src", ""))
        if not local or not Path(local).exists():
            raise RuntimeError(f"{kind}轨文件缺失：{track.get('name') or track.get('id')}")
        for clip, start, end in _visible_clips(track, base_duration if kind == "video" else 0.0):
            visual_counter += 1
            idx = add_input(local)
            tag = f"{visual_counter}_{idx}"
            if kind == "video":
                speed = max(0.5, min(2.0, float(clip.get("speed", 1.0) or 1.0)))
                # start/end 是时间轴坐标，不是源文件坐标；未提供映射时每个片段从源文件 0 秒开始。
                source_start = max(0.0, float(clip.get("source_start", 0.0) or 0.0))
                source_end = float(clip.get("source_end", source_start + (end - start) * speed)
                                   or source_start + (end - start) * speed)
                source_end = max(source_start + 0.01, source_end)
                chain = (f"[{idx}:v]trim=start={source_start:.3f}:end={source_end:.3f},"
                         f"setpts=(PTS-STARTPTS)/{speed:.6f}+{start:.3f}/TB")
                rotate = int(clip.get("rotate", 0) or 0) % 360
                if rotate == 90:
                    chain += ",transpose=1"
                elif rotate == 270:
                    chain += ",transpose=2"
                elif rotate == 180:
                    chain += ",transpose=1,transpose=1"
                if clip.get("flip_h"):
                    chain += ",hflip"
                if clip.get("flip_v"):
                    chain += ",vflip"
                source_w, source_h = _probe_res(local)
                chain += _crop_filter(clip.get("crop"), source_w, source_h)
                chain += (f",scale={width}:{height}:force_original_aspect_ratio=decrease,"
                          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=rgba")
                opacity = max(0.0, min(1.0, float(clip.get("opacity", 1.0) if clip.get("opacity") is not None else 1.0)))
                if opacity < 0.999:
                    chain += f",colorchannelmixer=aa={opacity:.3f}"
                layer = f"[layer_{tag}]"
                filters.append(chain + layer)
                current_video = overlay(current_video, layer, start, end, tag)

                # 未分离/未静音的视频自带声音严格跟随该视频片段与变速。
                if not track.get("muted_src_audio") and _has_audio(local):
                    achain = (f"[{idx}:a]atrim=start={source_start:.3f}:end={source_end:.3f},"
                              "asetpts=PTS-STARTPTS")
                    if abs(speed - 1.0) > 0.001:
                        achain += "," + pipeline._atempo_chain(speed)
                    achain += f",adelay={int(start * 1000)}:all=1[a_v_{tag}]"
                    filters.append(achain)
                    mix_labels.append(f"[a_v_{tag}]")
            else:
                scale = max(0.01, min(20.0, float(clip.get("scale", 1.0) or 1.0)))
                opacity = max(0.0, min(1.0, float(clip.get("opacity", 1.0) if clip.get("opacity") is not None else 1.0)))
                x = max(0.0, min(1.0, float(clip.get("x", 0.5) if clip.get("x") is not None else 0.5)))
                y = max(0.0, min(1.0, float(clip.get("y", 0.5) if clip.get("y") is not None else 0.5)))
                layer = f"[layer_{tag}]"
                # 预览图片基础宽度为画布的 30%，scale 是在该基准上的倍数。
                image_w = max(2, int(width * 0.30 * scale))
                filters.append(f"[{idx}:v]loop=loop=-1:size=1:start=0,trim=duration={end-start:.3f},"
                               f"setpts=PTS-STARTPTS+{start:.3f}/TB,scale={image_w}:-2,"
                               f"format=rgba,colorchannelmixer=aa={opacity:.3f}{layer}")
                out = f"[vis_{tag}]"
                # 前端坐标表示图片中心点（left/top + translate(-50%,-50%)）。
                ox = f"W*{x:.6f}-w/2"
                oy = f"H*{y:.6f}-h/2"
                filters.append(f"{current_video}{layer}overlay={ox}:{oy}:eof_action=pass:repeatlast=0:"
                               f"enable='between(t,{start:.3f},{end:.3f})'{out}")
                current_video = out

    # 普通音频轨：每个可见片段按时间放置，淡出按自身片段结束计算。
    for track in tracks:
        if track.get("kind") != "audio" or track.get("hidden") or track.get("muted"):
            continue
        source = (track.get("dub_src") if track.get("dubbed") and track.get("dub_src") else track.get("src")) or ""
        local = _local_of(jid, source)
        if not local or not Path(local).exists() or not _has_audio(local):
            raise RuntimeError(f"音频轨文件无效：{track.get('name') or track.get('id')}")
        source_duration = max(0.0, pipeline._probe_duration(local))
        clips = _visible_clips(track, source_duration)
        for clip_no, (clip, start, end) in enumerate(clips):
            source_start = max(0.0, float(clip.get("source_start", 0.0) or 0.0))
            clip_duration = min(end - start, max(0.0, source_duration - source_start)) if source_duration else end - start
            if clip_duration < 0.01:
                continue
            idx = add_input(local)
            volume = max(0.0, min(4.0, float(track.get("volume", 1.0) if track.get("volume") is not None else 1.0)))
            fade_in = max(0.0, min(clip_duration, float(track.get("fade_in", 0.0) or 0.0)))
            fade_out = max(0.0, min(clip_duration, float(track.get("fade_out", 0.0) or 0.0)))
            label = f"[a_t{idx}_{clip_no}]"
            chain = (f"[{idx}:a]atrim=start={source_start:.3f}:end={source_start+clip_duration:.3f},"
                     f"asetpts=PTS-STARTPTS,volume={volume:.6f}")
            if fade_in > 0:
                chain += f",afade=t=in:st=0:d={fade_in:.3f}"
            if fade_out > 0:
                chain += f",afade=t=out:st={max(0.0, clip_duration-fade_out):.3f}:d={fade_out:.3f}"
            chain += f",adelay={int(start * 1000)}:all=1{label}"
            filters.append(chain)
            mix_labels.append(label)

    audio_out = None
    if len(mix_labels) == 1:
        audio_out = mix_labels[0]
    elif mix_labels:
        filters.append("".join(mix_labels) +
                       f"amix=inputs={len(mix_labels)}:duration=longest:normalize=0,alimiter=limit=0.95[a_mix]")
        audio_out = "[a_mix]"

    # 所有可见字幕轨按前端顺序同时显示；隐藏轨/隐藏句不进入成片。
    sub_tracks = [t for t in tracks if t.get("kind") == "subtitle" and not t.get("hidden")]
    # 与前端默认样式保持一致：未保存样式时字幕不带黑色背景。
    export_style = {
        "font_size": 30, "color": "#FFFFFF", "bg": False,
        "bg_color": "#000000", "bg_opacity": 0.55, "bold": False, "outline": False,
        "position": "bottom", "x": 0.5, "y": 0.85, "font": "",
        "box_width": 0.92, "box_height": 0,
    }
    export_style.update(job.get("subtitle_style") or {})
    # ASS 的背景框由基础样式的 BorderStyle 决定；只要任一可见字幕行开启背景，
    # 就切换为盒式样式，逐行颜色/透明度仍由覆盖标签控制。
    if any(bool(row[3] and row[3].get("bg")) for row in _compose_visible_subtitles(sub_tracks, float(job.get("sub_shift", 0) or 0), export_style)):
        export_style["bg"] = True
    subtitle_rows = _compose_visible_subtitles(sub_tracks, float(job.get("sub_shift", 0) or 0),
                                               export_style)
    if subtitle_rows:
        # 圆角背景和文字写入同一个 ASS 字幕流。旧实现为每句创建一路全尺寸 SVG
        # 视频并串联 overlay，几十句字幕会让 ffmpeg 占用数 GB 内存并触发 OOM。
        ass_backgrounds = []
        for bg_no, (start, end, _text, row_style) in enumerate(subtitle_rows):
            st = dict(export_style)
            st.update(row_style or {})
            if not st.get("bg"):
                continue
            color = str(st.get("bg_color", "#000000")).lstrip("#")
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
                color = "000000"
            alpha = max(0.0, min(1.0, float(st.get("bg_opacity", 0.55) or 0.55)))
            box_w = max(1.0, min(float(width), float(st.get("box_width", 0.92) or 0.92) * width))
            wrapped_text = pipeline.wrap_subtitle_text(_text, st, width, height)
            line_count = max(1, len(wrapped_text.split("\n")))
            requested_h = (max(1.0, min(float(height), float(st.get("box_height", 0) or 0) * height))
                           if float(st.get("box_height", 0) or 0) > 0 else 0.0)
            # ``box_height`` 来自预览中的用户拖拽，导出必须逐像素遵守它，绝不能
            # 因字体或换行推测而把框再撑高。未手动设高时才按预览 CSS 的行高和
            # 上下内边距生成自然高度（这里不要用 ASS 的放大校准系数）。
            if requested_h:
                box_h = requested_h
            else:
                preview_font_px = max(1.0, float(st.get("font_size", 30) or 30) * height / 1080.0)
                box_h = min(float(height), preview_font_px * (1.25 * line_count + 0.24))
            cx = max(0.0, min(float(width), float(st.get("x", 0.5) or 0.5) * width))
            cy = max(0.0, min(float(height), float(st.get("y", 0.85) or 0.85) * height))
            left, top = max(0.0, cx - box_w / 2), max(0.0, cy - box_h / 2)
            radius = min(box_h * 0.25, box_w * 0.08)
            ass_backgrounds.append({
                "start": start, "end": end, "x": left, "y": top,
                "width": box_w, "height": box_h, "radius": radius,
                "color": f"#{color}", "opacity": alpha,
            })
        ass_path = jd / "export_timeline.ass"
        ass_style = dict(export_style, bg=False)
        ass_styles = [dict(style or export_style, bg=False) for _, _, _, style in subtitle_rows]
        pipeline.write_ass([(a, b, text) for a, b, text, _ in subtitle_rows], ass_path,
                           ass_style, width, height, styles=ass_styles,
                           backgrounds=ass_backgrounds)
        escaped = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        filters.append(f"{current_video}subtitles=filename='{escaped}'[v_sub]")
        current_video = "[v_sub]"

    # 删除所有视频均不可见的时间区间；画面、音频和字幕使用同一选择表达式，保持同步。
    compact_duration = sum(end - start for start, end in active_intervals)
    full_span = len(active_intervals) == 1 and active_intervals[0][0] <= 0.002 and abs(active_intervals[0][1] - duration) <= 0.05
    if not full_span:
        select_expr = "+".join(f"between(t,{start:.6f},{end:.6f})" for start, end in active_intervals)
        filters.append(f"{current_video}select='{select_expr}',setpts=N/FRAME_RATE/TB[v_out]")
        current_video = "[v_out]"
        if audio_out:
            filters.append(f"{audio_out}aselect='{select_expr}',asetpts=N/SR/TB[a_out_compact]")
            audio_out = "[a_out_compact]"
    else:
        compact_duration = duration

    _set_task_progress(jid, 20, "正在构建多轨时间轴…")
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", current_video]
    if audio_out:
        cmd += ["-map", audio_out, "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-t", f"{max(0.1, compact_duration):.3f}",
            "-progress", "pipe:1", "-nostats", str(output_path)]
    _set_task_progress(jid, 35, "正在合成视频")
    err_file = tempfile.NamedTemporaryFile(prefix="export_", suffix=".log", delete=False)
    err_path = err_file.name
    err_file.close()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=open(err_path, "w"),
                                text=True, bufsize=1)
        deadline = time.monotonic() + 1800
        last_ms = -1
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError("多轨导出超时（30分钟）")
            if line.startswith("out_time_ms="):
                try:
                    now_ms = int(line.split("=", 1)[1].strip())
                    if now_ms > last_ms:
                        last_ms = now_ms
                        ratio = min(1.0, max(0.0, now_ms / 1000000.0 / max(0.1, compact_duration)))
                        _set_task_progress(jid, 35 + int(ratio * 60), "正在合成视频")
                except ValueError:
                    pass
        returncode = proc.wait(timeout=max(1, int(deadline - time.monotonic())))
    finally:
        try:
            with open(err_path, "r", encoding="utf-8", errors="replace") as ef:
                stderr_text = ef.read()
        finally:
            Path(err_path).unlink(missing_ok=True)
    if returncode:
        tail = "\n".join(stderr_text.splitlines()[-12:])
        pipeline.log(f"[job {jid}] FFmpeg 多轨导出错误:\n{tail}")
        message = next((line.strip() for line in reversed(stderr_text.splitlines()) if line.strip()), "未知错误")
        raise RuntimeError(f"多轨导出失败：{message[:400]}")
    if not output_path.exists() or output_path.stat().st_size < 1024:
        raise RuntimeError("多轨导出未产生有效视频文件")
    _set_task_progress(jid, 94, "视频合成完成，正在准备文案…")
    _set(jid, status="exported", error="", final=f"/d/files/{jid}/{final_name}")


async def _synth_preview(jid, idx):
    jd = JOBS_DIR / jid
    (jd / "tts").mkdir(exist_ok=True)
    with LOCK:
        km_segs = jobs[jid].get("km_segments") or []
        if idx < 0 or idx >= len(km_segs):
            raise HTTPException(400, "行号越界")
        seg = km_segs[idx]
        lang = jobs[jid].get("target_lang", TARGET_LANG)
    voice, rate, pitch = tts_voice_profile(lang, seg["gender"])
    out = jd / "tts" / f"{idx:05d}.preview.mp3"
    c = edge_tts.Communicate(seg["km"], voice, rate=rate, pitch=pitch)
    await asyncio.wait_for(c.save(str(out)), timeout=120)
    return f"/d/files/{jid}/tts/{idx:05d}.preview.mp3"


# ---------- 路由 ----------
@router.get("/", include_in_schema=False)
@router.get("", include_in_schema=False)
def index():
    # no-store：强制浏览器每次重新拉取，避免用户一直看到旧版 HTML（导致"功能都没有"）
    return FileResponse(
        BASE / "static" / "index.html",
        headers={"Cache-Control": "no-store, max-age=0, must-revalidate"},
    )


def _blank_job(jid: str, title: str = "未命名项目", model: str = None):
    """创建空白项目；媒体后续仍只落在 ``output/jobs/{jid}/`` 内。"""
    return {"id": jid, "status": "uploaded", "source": "", "source_file": "", "media": [],
            "title": (title or "未命名项目").strip()[:180] or "未命名项目", "title_auto": True,
            "model": model or pipeline.WHISPER_MODEL, "target_lang": TARGET_LANG, "vocals_removed": False, "duration": 0.0,
            "video_clips": [], "dub_hidden": False, "audio_source": "none", "audio_muted": False,
            "orig_audio": "", "added_audio": "", "base_volume": 1.0, "dub_volume": 1.0,
            "fade_in": 0.0, "fade_out": 0.0, "sub_shift": 0.0, "subtitle_style": {},
            "cn_segments": [], "refined_segments": [], "km_segments": [], "tracks": [],
            "draft": "", "final": "", "km_srt": "", "error": ""}


@router.get("/api/projects")
def list_projects():
    """仅列出用户明确点过保存的项目；每条均关联到独立 job 目录。"""
    with LOCK:
        index = _read_projects_index()
        projects = []
        for value in index["projects"].values():
            job = jobs.get(value.get("id"))
            if not job:
                continue
            project = copy.deepcopy(value)
            subs = []
            for track in (job.get("tracks") or []):
                cues = track.get("cues") or []
                if track.get("kind") == "subtitle" and cues:
                    subs.append({"id": track.get("id", ""), "first_text": str(cues[0].get("text") or "")[:120]})
            selected_tid = str(job.get("project_subtitle_tid") or "")
            project["subtitle_tracks"] = subs
            project["selected_subtitle_tid"] = selected_tid if any(s["id"] == selected_tid for s in subs) else ""
            final = str(job.get("final") or "")
            project["download_url"] = final if final and Path(_local_of(job["id"], final)).is_file() else ""
            project["promo_copy"] = copy.deepcopy(job.get("promo_copy") or {})
            projects.append(project)
    projects.sort(key=lambda value: float(value.get("updated_at", 0) or 0), reverse=True)
    return {"projects": projects}


@router.put("/api/projects/{jid}/subtitle_track")
def select_project_subtitle_track(jid: str, body: ProjectSubtitleSelection):
    """保存项目库批量翻译/语音生成所使用的字幕轨；不触发任何处理任务。"""
    with LOCK:
        job = jobs.get(jid)
        if not job or jid not in _read_projects_index()["projects"]:
            raise HTTPException(404, "项目不存在或尚未保存")
        track = _find_track(job, body.tid)
        if not track or track.get("kind") != "subtitle" or not (track.get("cues") or []):
            raise HTTPException(400, "请选择有内容的字幕轨")
        job["project_subtitle_tid"] = body.tid
        project = _save_project_locked(job)
        _save_jobs()
    return {"ok": True, "selected_subtitle_tid": body.tid, "project": project}


@router.put("/api/projects/{jid}/batch_result")
def save_project_batch_result(jid: str, body: ProjectBatchResult):
    """记录项目库批量功能的最终结果；编辑页单独执行的任务绝不写这里。"""
    with LOCK:
        job = jobs.get(jid)
        if not job or jid not in _read_projects_index()["projects"]:
            raise HTTPException(404, "项目不存在或尚未保存")
        results = job.setdefault("project_batch_results", {})
        results[body.action] = {
            "state": body.state,
            "message": body.message.strip(),
            "updated_at": time.time(),
        }
        project = _save_project_locked(job)
        _save_jobs()
    pipeline.log(f"[job {jid}] project_batch_result action={body.action} state={body.state}")
    return {"ok": True, "batch_results": project.get("batch_results", {})}


@router.post("/api/projects/new")
def new_project(body: dict = None):
    title = str((body or {}).get("title") or "未命名项目")
    jid = uuid.uuid4().hex[:12]
    (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    with LOCK:
        jobs[jid] = _blank_job(jid, title)
        _save_jobs()
    pipeline.log(f"[job {jid}] project_new title={jobs[jid]['title']}")
    return {"id": jid, "status": "uploaded", "title": jobs[jid]["title"]}


def _delete_projects(ids: list[str]):
    """删除项目索引、任务状态和对应独立目录；任务运行时拒绝删除以避免竞态。"""
    clean_ids = list(dict.fromkeys(str(jid) for jid in ids if re.fullmatch(r"[0-9a-f]{12}", str(jid))))
    if not clean_ids:
        raise HTTPException(400, "请选择要删除的项目")
    with LOCK:
        index = _read_projects_index()
        active = [jid for jid in clean_ids if (jobs.get(jid) or {}).get("_task")]
        if active:
            raise HTTPException(409, "选中的项目仍有后台任务运行，请等待任务结束后再删除")
        existing = [jid for jid in clean_ids if jid in index["projects"] and jid in jobs]
        if not existing:
            raise HTTPException(404, "项目不存在或尚未保存")
        removed = [{"id": jid, "title": str(jobs[jid].get("title") or "未命名项目")} for jid in existing]
        for jid in existing:
            index["projects"].pop(jid, None)
            jobs.pop(jid, None)
        _atomic_json_write(PROJECTS_FILE, index)
        _save_jobs()
    # 仅使用由 12 位任务 ID 确定的子目录，绝不处理 jobs 根目录或其它项目。
    for jid in existing:
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=False)
        pipeline.log(f"[job {jid}] project_deleted")
    return removed


@router.delete("/api/projects/{jid}")
def delete_project(jid: str):
    return {"ok": True, "deleted": _delete_projects([jid])}


@router.delete("/api/projects")
def delete_projects(req: ProjectsDeleteRequest):
    return {"ok": True, "deleted": _delete_projects(req.ids)}


@router.post("/api/jobs/{jid}/save_project")
def save_project(jid: str):
    """持久化项目清单。媒体不复制，项目文件与全部素材同目录保存。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        project = _save_project_locked(jobs[jid])
        _save_jobs()
    pipeline.log(f"[job {jid}] project_saved title={project['title']} files={project['file_count']}")
    return {"ok": True, "project": project}


@router.post("/api/jobs/upload")
def upload_job(file: UploadFile = File(...), model: str = None):
    jid = uuid.uuid4().hex[:12]
    jd = JOBS_DIR / jid
    jd.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename)
    kind = _media_kind(safe_name)
    # 首个视频使用兼容旧任务的 source.mp4；后续媒体必须使用独立文件名，不能覆盖主视频。
    try:
        meta = _ingest(file, jid, kind, primary=(kind == "video"))
    except Exception:
        # 上传失败（如空间不足）不留下空任务目录或半截文件。
        shutil.rmtree(jd, ignore_errors=True)
        raise
    dur = meta["dur"]
    asset = meta["asset"]
    # 不自动加时间轴轨：媒体先进「媒体库」，用户从媒体库手动「＋」添加到时间轴（需求：上传不自动进时间轴）
    media = [asset]
    with LOCK:
        jobs[jid] = {"id": jid, "status": "uploaded", "source": "",
                     # 兼容旧逻辑：source_file 仅在有视频轨时由「添加到时间轴」写入
                     "source_file": "",
                     "media": media,
                     "title": (Path(safe_name).stem or "未命名项目"), "title_auto": True,
                     "model": model or pipeline.WHISPER_MODEL, "target_lang": TARGET_LANG,
                     "vocals_removed": False, "duration": dur or (asset.get("clips") or [{}])[0].get("end", 0),
                     # 旧字段保留（旧任务兼容 / 旧导出回退分支），新流程以 tracks 为准
                     "video_clips": [{"start": 0.0, "end": dur, "hidden": False,
                                     "rotate": 0, "flip_h": False, "flip_v": False,
                                     "crop": None, "speed": 1.0}],
                     "dub_hidden": False,
                     "audio_source": "none", "audio_muted": False,
                     "orig_audio": "", "added_audio": "",
                     "base_volume": 1.0, "dub_volume": 1.0,
                     "fade_in": 0.0, "fade_out": 0.0,
                     "sub_shift": 0.0,
                     "subtitle_style": {},
                     "cn_segments": [], "refined_segments": [], "km_segments": [],
                     "tracks": [],
                     "draft": "", "final": "", "km_srt": "", "error": ""}
        _save_jobs()
    return {"id": jid, "status": "uploaded"}


def _ingest(file, jid, kind, primary=False):
    """保存上传文件到 job 目录，返回 {local, url, dur, cues, track}。"""
    jd = JOBS_DIR / jid
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "media").mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(file.filename)
    detected_kind = _media_kind(safe_name)
    if kind not in {"video", "audio", "subtitle", "image"} or kind != detected_kind:
        raise HTTPException(400, "文件扩展名与媒体类型不匹配")
    fname = f"{jid}_{safe_name}"
    if kind == "video" and primary:
        dest = jd / "source.mp4"
        fname = "source.mp4"
    else:
        # 去掉可能的前缀冲突，统一放 media 目录
        dest = jd / "media" / fname
    _save_upload(file, dest)
    local = str(dest)
    url = _files_url(jid, dest.relative_to(jd).as_posix())
    dur = 0.0
    cues = []
    if kind in ("video", "audio"):
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=noprint_wrappers=1:nokey=1", str(dest)],
                                capture_output=True, text=True, timeout=30)
            dur = float((pr.stdout or "").strip() or 0)
        except Exception:
            dur = 0.0
    if kind == "subtitle":
        cues = _parse_subtitle_file(dest)
    tid = _new_track_id({"tracks": []})
    if kind == "video":
        asset = {"id": tid, "kind": "video", "name": Path(safe_name).stem or "视频", "hidden": False, "muted": False,
                 "src": url, "lang": "zh", "role": "",
                 "clips": [{"start": 0.0, "end": dur, "hidden": False}],
                 "cues": [], "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                 "dubbed": False, "dub_src": "", "dub_lang": ""}
    elif kind == "audio":
        asset = {"id": tid, "kind": "audio", "name": Path(safe_name).stem or "音频", "hidden": False, "muted": False,
                 "src": url, "lang": "zh", "role": "",
                 "clips": [{"start": 0.0, "end": dur, "hidden": False}],
                 "cues": [], "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                 "dubbed": False, "dub_src": "", "dub_lang": ""}
    elif kind == "subtitle":
        sub_lang = "zh" if safe_name.lower().find("translate") < 0 else TARGET_LANG
        asset = {"id": tid, "kind": "subtitle", "name": Path(safe_name).stem or "字幕", "hidden": False, "muted": False,
                 "src": url, "lang": sub_lang, "role": "import",
                 "clips": [], "cues": cues, "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                 "dubbed": False, "dub_src": "", "dub_lang": ""}
    else:  # image
        asset = {"id": tid, "kind": "image", "name": Path(safe_name).stem or "图片",
                 "hidden": False, "muted": False, "src": url, "lang": "zh", "role": "",
                 "clips": [{"start": 0.0, "end": 5.0, "hidden": False,
                            "x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0}],
                 "cues": [], "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                 "dubbed": False, "dub_src": "", "dub_lang": ""}
    return {"local": local, "url": url, "dur": dur, "cues": cues, "asset": asset}


def _sync_primary_video(job):
    """同步旧版单视频字段到时间轴第一条视频，避免新增视频覆盖主源。"""
    first = next((t for t in (job.get("tracks") or []) if t.get("kind") == "video"), None)
    if first:
        job["source"] = _local_of(job["id"], first.get("src", ""))
        job["source_file"] = first.get("src", "")
    else:
        job["source"] = ""
        job["source_file"] = ""


@router.post("/api/jobs/{jid}/media")
def add_media(jid: str, file: UploadFile = File(...), kind: str = ""):
    """向已有任务追加媒体到「媒体库」（不自动加时间轴轨；用户从媒体库手动「＋」添加）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
    k = kind or _media_kind(_safe_filename(file.filename))
    meta = _ingest(file, jid, k, primary=False)
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        asset = meta["asset"]
        media = jobs[jid].get("media") or []
        # 项目库的新建项目先是空任务，首次上传会走这里而不是 /upload。
        # 与旧路径保持一致：仅在系统自动命名状态下，以第一份媒体文件名命名。
        if not media and jobs[jid].get("title_auto", jobs[jid].get("title") in ("", "未命名项目")):
            jobs[jid]["title"] = str(asset.get("name") or "未命名项目")[:180]
            jobs[jid]["title_auto"] = True
        media.append(asset)
        jobs[jid]["media"] = media
        if not jobs[jid].get("duration"):
            # 首传非视频（如图片）时，用该轨片段时长兜底，避免时间轴时长缺失
            _cd = asset.get("dur") or (asset.get("clips") or [{}])[0].get("end", 0)
            if _cd: jobs[jid]["duration"] = _cd
        _save_jobs()
    return {"ok": True, "asset": asset, "media": jobs[jid]["media"]}


@router.get("/api/jobs")
def list_jobs():
    with LOCK:
        return [{"id": j["id"], "status": j["status"], "model": j.get("model"),
                 "target_lang": j.get("target_lang"),
                 "vocals_removed": j.get("vocals_removed"),
                 "final": j.get("final"), "draft": j.get("draft")} for j in jobs.values()]


@router.get("/api/jobs/{jid}")
def get_job(jid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        return copy.deepcopy(_normalize(jobs[jid]))


@router.put("/api/jobs/{jid}/tracks")
def save_tracks(jid: str, body: TracksUpdate):
    """前端保存整条多轨时间轴（编辑后统一回写）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jobs[jid]["tracks"] = [track.model_dump() for track in body.tracks]
        _save_jobs()
        result = copy.deepcopy(jobs[jid]["tracks"])
    return {"ok": True, "tracks": result}


@router.post("/api/jobs/{jid}/tracks")
def add_track_from_media(jid: str, body: AddTrackReq):
    """从媒体库把某条媒体添加为时间轴轨道（视频自动补基础音频轨；音频/字幕/图片直接成轨）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        job = jobs[jid]
        media = job.get("media") or []
        asset = next((m for m in media if m.get("id") == body.media_id), None)
        if not asset:
            raise HTTPException(400, "媒体不存在，请先上传")
        tr = copy.deepcopy(asset)
        tr["id"] = _new_track_id(job)
        tr.setdefault("hidden", False)
        tr.setdefault("muted", False)
        tr.setdefault("volume", 1.0)
        tr.setdefault("fade_in", 0.0)
        tr.setdefault("fade_out", 0.0)
        tr.setdefault("dubbed", False)
        tr.setdefault("dub_src", "")
        tr.setdefault("dub_lang", "")
        tracks = job.get("tracks") or []
        tracks.append(tr)
        if asset["kind"] == "video":
            # 视频只加「视频轨」——不自动补音频轴（需求：不自动分离音频）。
            # 视频自带音轨默认随画面一起导出；用户点视频轨小操作框「分离原声」后才出现独立音频轨。
            if not job.get("duration"):
                job["duration"] = asset.get("dur") or (tr.get("clips") or [{}])[0].get("end", 0)
        job["tracks"] = tracks
        _sync_primary_video(job)
        _save_jobs()
    return {"ok": True, "track": tr, "tracks": job["tracks"]}


@router.delete("/api/jobs/{jid}/tracks/{tid}")
def delete_track(jid: str, tid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        tracks = jobs[jid].get("tracks") or []
        del_tr = next((t for t in tracks if t.get("id") == tid), None)
        # 删除该轨时，若正有针对它的分离/去人声后台任务在跑，立即标记中止，避免结果被绑回
        if del_tr:
            _abort_task(jid, tid)
        tracks = [t for t in tracks if t.get("id") != tid]
        # 删掉「分离原声」产生的音频轨：只移除该音频轨，不再自动改动视频源音（保持删除行为可预期，杜绝"删音频冒视频"）
        # 删视频轨时，连带删掉它派生的分离音频轨，避免留下孤儿音频造成混淆
        if del_tr and del_tr.get("kind") == "video":
            tracks = [t for t in tracks if t.get("from_track") != tid]
        jobs[jid]["tracks"] = tracks
        _sync_primary_video(jobs[jid])
        _save_jobs()
    return {"ok": True, "tracks": jobs[jid]["tracks"]}


@router.delete("/api/jobs/{jid}/media/{mid}")
def delete_media(jid: str, mid: str):
    """删除媒体库中的一条素材：同时删除其磁盘文件，以及由它派生的时间轴轨道（删素材 = 删整根轴）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        job = jobs[jid]
        media = job.get("media") or []
        asset = next((m for m in media if m.get("id") == mid), None)
        if not asset:
            raise HTTPException(404, "素材不存在")
        # 删除磁盘文件
        local = _local_of(jid, asset.get("src", ""))
        if local and Path(local).exists():
            try:
                Path(local).unlink()
            except Exception:
                pass
        # 从媒体库移除
        job["media"] = [m for m in media if m.get("id") != mid]
        # 删除派生轨道（深拷贝进轨时 src 同源，按 src 匹配整根轴）
        src = asset.get("src", "")
        if src:
            tracks = job.get("tracks") or []
            tracks = [t for t in tracks if t.get("src") != src]
            job["tracks"] = tracks
            _sync_primary_video(job)
        _save_jobs()
    return {"ok": True, "media": job["media"], "tracks": job.get("tracks")}


def _find_track(job, tid):
    for t in (job.get("tracks") or []):
        if t.get("id") == tid:
            return t
    return None


def _abort_task(jid, tid=None):
    """标记当前运行中的后台任务中止（用户删除相关轨道时调用，避免后台结果被绑回时间轴）。"""
    job = jobs.get(jid)
    if not job:
        return
    t = job.get("_task")
    if not t:
        return
    if tid is None or t.get("tid") == tid:
        t["aborted"] = True
        job["status"] = t.get("prev_status") or "uploaded"


def _task_can_commit_locked(job):
    task_id = getattr(TASK_CONTEXT, "task_id", None)
    if not task_id:
        return True
    task = job.get("_task") or {}
    return task.get("id") == task_id and not task.get("aborted")


def _step_separate(jid, tid):
    """从视频轨抽取音轨，作为新音频轨追加（不自动、仅用户点击触发；可被删除/中止取消）。"""
    jd = JOBS_DIR / jid
    with LOCK:
        job = jobs[jid]
        t = _find_track(job, tid)
        if not t or t["kind"] != "video":
            raise RuntimeError("仅视频轨可分离音频")
        prev_status = (job.get("_task") or {}).get("prev_status", job.get("status"))
        local = _local_of(jid, t["src"])
    out = jd / f"audio_{tid}.wav"
    # 保留立体声：便于后续「去除人声」基于中置抵消；配音为 TTS 不依赖音频内容
    try:
        _set_task_progress(jid, 5, "步骤 1/3：正在分离音频…")
        subprocess.run(["ffmpeg", "-y", "-i", local, "-vn", "-ac", "2", "-ar", "44100", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    except Exception:
        with LOCK:
            job = jobs[jid]
            if (job.get("_task") or {}).get("tid") == tid:
                job["status"] = prev_status
        raise
    with LOCK:
        job = jobs[jid]
        tk = job.get("_task") or {}
        # 用户已删除该视频轨/片段或主动中止：丢弃结果，避免「删视频/删片段后冒出孤儿音频」
        if tk.get("aborted") or tk.get("tid") != tid:
            try:
                if out.exists(): out.unlink()
            except Exception:
                pass
            if tk.get("tid") == tid:
                job["status"] = prev_status
            return None
        src = _find_track(job, tid)
        if not src or src.get("kind") != "video":
            try:
                if out.exists(): out.unlink()
            except Exception:
                pass
            return None
        url = _files_url(jid, out.relative_to(jd).as_posix())
        at = {"id": _new_track_id(job), "kind": "audio", "name": f"{src.get('name','视频')}·音频",
              "hidden": False, "muted": False, "src": url, "lang": "zh", "role": "separated",
              "from_track": tid,
              "clips": [{"start": 0.0, "end": job.get("duration") or 0, "hidden": False}],
              "cues": [], "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
              "dubbed": False, "dub_src": "", "dub_lang": ""}
        tracks = job.get("tracks") or []
        tracks.append(at)
        # 二次确认中止：若 append 之后才被标记中止，连刚写入的音频轨一并撤销
        if (job.get("_task") or {}).get("aborted"):
            tracks.pop()
            try:
                if out.exists(): out.unlink()
            except Exception:
                pass
            job["tracks"] = tracks
            job["status"] = prev_status
            _save_jobs()
            return None
        # 标记原视频自带音静音：避免与分离出的音轨叠加成双倍原声（删掉该分离轨时不再自动恢复，保持可预期）
        for vt in tracks:
            if vt.get("id") == tid:
                vt["muted_src_audio"] = True
        job["tracks"] = tracks
        job["status"] = prev_status
        _save_jobs()
    _set_task_progress(jid, 100, "步骤 1/3：音频分离完成")
    return at


@router.post("/api/jobs/{jid}/tracks/{tid}/separate")
def separate_track(jid: str, tid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "separate", tid)
    threading.Thread(target=_run_thread, args=(_step_separate, jid, task_id, tid), daemon=True).start()
    return {"ok": True, "status": "running"}


def _quick_make_range(jid, start, end, message):
    with LOCK:
        task = (jobs.get(jid) or {}).get("_task") or {}
        if not _task_can_commit_locked(jobs.get(jid) or {}):
            return False
        task["progress_from"] = start
        task["progress_to"] = end
        task["message"] = message
        task["progress"] = start
        # 子步骤会沿用各自的历史状态；工作流未完成时必须保持 running，前端才会持续轮询。
        jobs[jid]["status"] = "running"
        _save_jobs()
        return True


def _step_quick_make(jid, tid, model):
    """一键制作：严格串行复用分离、轨道识别和去人声三个已验证步骤。"""
    if not _quick_make_range(jid, 0, 30, "步骤 1/3：准备分离音频…"):
        return None
    audio_track = _step_separate(jid, tid)
    if not audio_track:
        return None
    audio_tid = audio_track["id"]
    with LOCK:
        task = jobs[jid].get("_task") or {}
        if task.get("aborted"):
            return None
        # 工作流在视频菜单中发起，但后两步实际作用于刚分离的音频轨。
        task["target_tid"] = audio_tid
        _save_jobs()
    if not _quick_make_range(jid, 30, 65, "步骤 2/3：准备生成字幕…"):
        return None
    _step_asr_track(jid, audio_tid, model)
    if not _quick_make_range(jid, 65, 100, "步骤 3/3：准备去除人声…"):
        return None
    return _step_remove_vocals_track(jid, audio_tid)


@router.post("/api/jobs/{jid}/tracks/{tid}/quick_make")
def quick_make_track(jid: str, tid: str, model: str = None):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        track = _find_track(jobs[jid], tid)
        if not track or track.get("kind") != "video":
            raise HTTPException(400, "一键制作仅适用于视频轨")
        task_id = _begin_task_locked(jobs[jid], "quick_make", tid)
    threading.Thread(target=_run_thread, args=(_step_quick_make, jid, task_id, tid, model or pipeline.WHISPER_MODEL), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.post("/api/jobs/{jid}/task/abort")
def abort_task(jid: str):
    """中止当前运行中的后台任务（删除相关轨道时由前端调用，防止结果被绑回）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        _abort_task(jid)
        _save_jobs()
    return {"ok": True}


@router.post("/api/jobs/{jid}/tracks/{tid}/vocals")
def remove_vocals_track(jid: str, tid: str):
    """音频轨去除人声（仅作用于该音频轨，不影响其它轨；可被删除/中止取消）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        job = jobs[jid]
        t = _find_track(job, tid)
        if not t or t["kind"] != "audio":
            raise HTTPException(400, "仅音频轨可去人声")
        task_id = _begin_task_locked(job, "vocals", tid)
    threading.Thread(target=_run_thread, args=(_step_remove_vocals_track, jid, task_id, tid), daemon=True).start()
    return {"ok": True, "status": "running"}


def _step_remove_vocals_track(jid, tid):
    """对该音频轨做去人声：优先 demucs（高质量），不可用时退化为 ffmpeg 中置抵消（需立体声）。"""
    jd = JOBS_DIR / jid
    with LOCK:
        job = jobs[jid]
        t = _find_track(job, tid)
        local = _local_of(jid, (t["dubbed"] and t.get("dub_src")) or t["src"])
    out = jd / f"novocals_{tid}.wav"
    try:
        # 优先 demucs（人声/伴奏分离）
        _set_task_progress(jid, 5, "正在准备音频…")
        tmp = jd / "_vr_src.wav"
        subprocess.run(["ffmpeg", "-y", "-i", local, "-vn", "-ac", "2", "-ar", "44100", str(tmp)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        _set_task_progress(jid, 12, "正在分析人声与伴奏…")
        # demucs 本身不提供可读取的百分比。进程仍存活时按媒体时长给出保守的估算进度，
        # 并在关键阶段更新文字，绝不把未完成任务显示为 100%。
        proc = subprocess.Popen([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
                                 "-o", str(jd / "demucs_track"), str(tmp)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        estimated_seconds = max(30.0, float(job.get("duration") or 0) * 1.5)
        while proc.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > 600:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise subprocess.TimeoutExpired(proc.args, 600)
            progress = min(90, 12 + int(78 * min(elapsed / estimated_seconds, 1.0)))
            _set_task_progress(jid, progress, "正在去除人声（估算进度）…")
            time.sleep(0.5)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)
        _set_task_progress(jid, 94, "正在整理伴奏文件…")
        nv = jd / "demucs_track" / "htdemucs" / "_vr_src" / "no_vocals.wav"
        if not nv.exists():
            raise RuntimeError("demucs 未产出 no_vocals")
        shutil.copy(nv, out)
    except (FileNotFoundError, subprocess.CalledProcessError):
        # demucs 不可用或失败：退化为 ffmpeg 中置抵消（仅对立体声有效；单声道会趋于静音）
        subprocess.run(["ffmpeg", "-y", "-i", local, "-af",
                        "pan=stereo|c0=c0-c1|c1=c1-c0", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    _set_task_progress(jid, 98, "正在保存到时间轴…")
    with LOCK:
        job = jobs[jid]
        t = _find_track(job, tid)
        tk = job.get("_task") or {}
        # 用户已删除该音频轨或主动中止：丢弃去人声结果
        if not t or tk.get("aborted") or (tk.get("tid") != tid and tk.get("target_tid") != tid):
            try:
                if out.exists(): out.unlink()
            except Exception:
                pass
            if tk.get("tid") == tid:
                job["status"] = tk.get("prev_status", job.get("status"))
            return None
        url = _files_url(jid, out.relative_to(jd).as_posix())
        t["src_orig"] = t.get("src")
        t["src"] = url
        t["vocals_removed"] = True
        t["name"] = (t.get("name") or "音频") + "·去人声"
        restored = "dubbed" if job.get("km_segments") else ("refined" if job.get("refined_segments") else "asr_done")
        job["status"] = restored
        _save_jobs()
    _set_task_progress(jid, 100, "去人声完成")
    return {"ok": True, "track": t, "tracks": jobs[jid]["tracks"]}


def _step_asr_track(jid, tid, model):
    with LOCK:
        job = jobs[jid]
        t = _find_track(job, tid)
        if not t:
            raise RuntimeError("轨道不存在")
        local = _local_of(jid, t["src"])
        duration = float(job.get("duration") or 0)
    _set_task_progress(jid, 3, "正在加载语音识别模型…")

    def on_asr_progress(position):
        # Whisper 按音频时间顺序产出片段；这里是已识别媒体时长的真实比例。
        ratio = min(1.0, position / duration) if duration > 0 else 0.0
        _set_task_progress(jid, 8 + int(ratio * 84), "正在识别字幕…")

    cn = pipeline.asr(local, model_size=model, progress_cb=on_asr_progress)
    if not cn:
        raise RuntimeError("ASR 未识别到任何台词")
    _set_task_progress(jid, 94, "正在整理字幕时间轴…")
    segs = [{"index": i, "start": s, "end": e, "zh": t} for i, (s, e, t) in enumerate(cn)]
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        tracks = job.get("tracks") or []
        # 更新/新建 role=source 字幕轨
        st = next((x for x in tracks if x.get("kind") == "subtitle" and x.get("role") == "source"), None)
        cues = [{"start": s, "end": e, "text": tx, "hidden": False, "style": None}
                for s, e, tx in cn]
        if st:
            st["cues"] = cues
        else:
            tracks.append({"id": _new_track_id(job), "kind": "subtitle", "name": "字幕",
                           "hidden": False, "muted": False, "src": "", "lang": "zh", "role": "source",
                           "clips": [], "cues": cues, "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                           "dubbed": False, "dub_src": "", "dub_lang": ""})
        job["tracks"] = tracks
        # 同步旧字段，便于旧导出/预览回退
        job["cn_segments"] = segs
        job["refined_segments"] = segs
        job["status"] = "asr_done"
        _save_jobs()
    _set_task_progress(jid, 100, "字幕生成完成")


@router.post("/api/jobs/{jid}/tracks/{tid}/asr")
def asr_track(jid: str, tid: str, model: str = None):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "track_asr", tid)
    threading.Thread(target=_run_thread, args=(_step_asr_track, jid, task_id, tid, model or pipeline.WHISPER_MODEL), daemon=True).start()
    return {"ok": True, "status": "running"}


def _step_translate_track(jid, tid):
    with LOCK:
        job = jobs[jid]
        t = _find_track(job, tid)
        if not t or t["kind"] != "subtitle":
            raise RuntimeError("仅字幕轨可翻译")
        cues = t.get("cues") or []
        lang = job.get("target_lang", TARGET_LANG)
        default_gender = job.get("default_gender", DEFAULT_GENDER)
    if not cues:
        raise RuntimeError("字幕轨为空，无法翻译")
    parsed = [(cue_voice(c, default_gender), parse_gender(c.get("text", ""), default_gender)[1]) for c in cues]
    clean = [text for _, text in parsed]
    _set_task_progress(jid, 8, "正在翻译字幕…")
    def _translation_progress(ratio):
        _set_task_progress(jid, 8 + int(max(0, min(100, ratio)) * 0.84), "正在翻译字幕…")
    # pipeline 内部已经负责行数校验和逐句回退，不再重复发起整批请求。
    km = pipeline.translate_lines(clean, "", LANG_NAME.get(lang, lang), progress_cb=_translation_progress)
    if len(km) != len(clean):
        raise RuntimeError(f"翻译结果行数不匹配：输入 {len(clean)} 行，输出 {len(km)} 行")
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        tracks = job.get("tracks") or []
        tr = next((x for x in tracks if x.get("kind") == "subtitle" and x.get("role") == "translate"), None)
        tcues = [{"start": c["start"], "end": c["end"], "text": km[i], "voice": parsed[i][0], "hidden": False, "style": None}
                 for i, c in enumerate(cues)]
        if tr:
            tr["cues"] = tcues
            tr["lang"] = lang
        else:
            tracks.append({"id": _new_track_id(job), "kind": "subtitle", "name": "字幕",
                           "hidden": False, "muted": False, "src": "", "lang": lang, "role": "translate",
                           "clips": [], "cues": tcues, "volume": 1.0, "fade_in": 0.0, "fade_out": 0.0,
                           "dubbed": False, "dub_src": "", "dub_lang": ""})
        job["tracks"] = tracks
        job["km_segments"] = [{"index": i, "start": c["start"], "end": c["end"], "zh": c.get("text", ""),
                               "km": km[i], "gender": parsed[i][0]} for i, c in enumerate(cues)]
        _save_jobs()
    _set_task_progress(jid, 100, "翻译字幕完成")


@router.post("/api/jobs/{jid}/tracks/{tid}/translate")
def translate_track(jid: str, tid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "track_translate", tid)
    threading.Thread(target=_run_thread, args=(_step_translate_track, jid, task_id, tid), daemon=True).start()
    return {"ok": True, "status": "running"}


def _step_dub_track(jid, tid):
    """按音轨配音：取一条字幕轨文本 → TTS(音轨语言) → 生成该音轨的配音音源。"""
    jd = JOBS_DIR / jid
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        at = _find_track(job, tid)
        if not at or at["kind"] != "audio":
            raise RuntimeError("仅音频轨可配音")
        # 选字幕轨：优先 source，其次 translate，再次任意 subtitle
        subs = [x for x in (job.get("tracks") or []) if x.get("kind") == "subtitle"]
        src_sub = next((x for x in subs if x.get("role") == "source"), None) or \
                  next((x for x in subs if x.get("role") == "translate"), None) or (subs[0] if subs else None)
        if not src_sub or not src_sub.get("cues"):
            raise RuntimeError("尚无字幕可配音，请先生成字幕")
        lang = at.get("dub_lang") or job.get("target_lang", TARGET_LANG)
        cues = src_sub["cues"]
        default_gender = job.get("default_gender", DEFAULT_GENDER)
    voice_map = VOICE_MAP.get(lang, VOICE_MAP[TARGET_LANG])
    parsed = []
    for c in cues:
        gender, clean = cue_voice(c, default_gender), parse_gender(c.get("text", ""), default_gender)[1]
        parsed.append({"start": c["start"], "end": c["end"], "clean": clean, "gender": gender})
    _set_task_progress(jid, 5, "正在翻译字幕…")
    km = pipeline.translate_lines([p["clean"] for p in parsed], "", LANG_NAME.get(lang, lang),
                                  progress_cb=lambda p: _set_task_progress(jid, 5 + int(p * .35), "正在翻译字幕…")) \
        if lang != "zh" else [p["clean"] for p in parsed]
    if len(km) != len(parsed):
        raise RuntimeError(f"翻译结果行数不匹配：输入 {len(parsed)} 行，输出 {len(km)} 行")
    (jd / "tts").mkdir(exist_ok=True)
    tts_tmp = jd / "tts"
    wavs = []
    for i, p in enumerate(parsed):
        _set_task_progress(jid, 10 + int(i * 75 / max(1, len(parsed))), f"正在生成配音 {i + 1}/{len(parsed)}…")
        voice, rate, pitch = tts_voice_profile(lang, p["gender"])
        mp3 = tts_tmp / f"dub_{tid}_{i:05d}.mp3"
        txt = km[i] if lang != "zh" else p["clean"]
        if not (mp3.exists() and mp3.stat().st_size > 100):
            pipeline.synth_edge(txt, voice, str(mp3), rate=rate, pitch=pitch)
        wav = tts_tmp / f"dub_{tid}_{i:05d}.fit.wav"
        pipeline._fit_clip(str(mp3), str(wav), max(p["end"] - p["start"], 0.0))
        wavs.append((p["start"], p["end"], str(wav)))
    duration = max((p["end"] for p in parsed), default=0)
    dub = pipeline.build_dub_audio(wavs, duration, str(tts_tmp))
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        at = _find_track(job, tid)
        if not at:
            return None
        url = _files_url(jid, Path(dub).relative_to(jd).as_posix())
        at["dubbed"] = True
        at["dub_src"] = url
        at["dub_lang"] = lang
        job["dub_wav"] = str(dub)
        _save_jobs()


@router.post("/api/jobs/{jid}/tracks/{tid}/dub")
def dub_track(jid: str, tid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "track_dub", tid)
    threading.Thread(target=_run_thread, args=(_step_dub_track, jid, task_id, tid), daemon=True).start()
    return {"ok": True, "status": "running"}


def _step_synthesize_subtitle(jid, tid):
    """把人工确认后的目标语言字幕直接合成为独立语音轨，不再翻译一次。"""
    jd = JOBS_DIR / jid
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        sub = _find_track(job, tid)
        if not sub or sub.get("kind") != "subtitle":
            raise RuntimeError("请选择要生成语音的字幕轨")
        cues = [c for c in (sub.get("cues") or []) if not c.get("hidden") and (c.get("text") or "").strip()]
        lang = sub.get("lang") or ("zh" if sub.get("role") == "source" else job.get("target_lang", TARGET_LANG))
        default_gender = job.get("default_gender", DEFAULT_GENDER)
    if not cues:
        raise RuntimeError("翻译字幕为空，无法生成语音")
    # Edge 的语种语音不会可靠地朗读另一种文字，常见表现就是模糊的
    # “No audio was received”。先在整轨开始前校验，避免已成功的前半轨
    # 被无谓地重复请求，也把真正需要修正的字幕行明确告诉用户。
    if lang == "km":
        invalid = []
        for index, cue in enumerate(cues, start=1):
            clean = parse_gender(cue.get("text", ""))[1].strip()
            if clean and not re.search(r"[\u1780-\u17ff]", clean):
                invalid.append(index)
        if invalid:
            listed = "、".join(str(n) for n in invalid[:5])
            more = "…" if len(invalid) > 5 else ""
            raise RuntimeError(
                f"目标字幕第 {listed} 句{more}不是高棉语文字；请先翻译或改为高棉文后再生成语音"
            )
    voice_map = VOICE_MAP.get(lang, VOICE_MAP[TARGET_LANG])
    tts_tmp = jd / "tts"
    tts_tmp.mkdir(exist_ok=True)
    wavs = []
    total = len(cues)
    for i, cue in enumerate(cues):
        _set_task_progress(jid, int(i * 90 / max(1, total)), f"正在生成语音 {i + 1}/{total}…")
        gender, text = cue_voice(cue, default_gender), parse_gender(cue.get("text", ""), default_gender)[1]
        if not text.strip():
            continue
        voice, rate, pitch = tts_voice_profile(lang, gender)
        mp3 = tts_tmp / f"subtitle_dub_{tid}_{i:05d}.mp3"
        if mp3.exists() and mp3.stat().st_size > 100:
            pipeline.log(f"语音生成 {i + 1}/{total} 复用已完成音频 声线={gender} voice={voice}")
        else:
            pipeline.log(f"语音生成 {i + 1}/{total} 声线={gender} voice={voice}")
            try:
                pipeline.synth_edge(text, voice, str(mp3), rate=rate, pitch=pitch)
            except Exception as ex:
                label = {"female": "女声1", "female2": "女声2", "male": "男声1", "male2": "男声2",
                         "narration": "旁白1", "narration2": "旁白2"}.get(gender, gender)
                raise RuntimeError(f"第 {i + 1} 句{label}生成失败（{voice}）：{ex}") from ex
        wav = tts_tmp / f"subtitle_dub_{tid}_{i:05d}.fit.wav"
        pipeline._fit_clip(str(mp3), str(wav), max(float(cue["end"]) - float(cue["start"]), 0.1))
        wavs.append((float(cue["start"]), float(cue["end"]), str(wav)))
    if not wavs:
        raise RuntimeError("翻译字幕没有可合成的文本")
    _set_task_progress(jid, 94, "正在合成语音轨…")
    duration = max(end for _, end, _ in wavs)
    dub = pipeline.build_dub_audio(wavs, duration, str(tts_tmp))
    with LOCK:
        job = jobs[jid]
        if not _task_can_commit_locked(job):
            return None
        url = _files_url(jid, Path(dub).relative_to(jd).as_posix())
        tracks = job.get("tracks") or []
        audio = next((t for t in tracks if t.get("kind") == "audio" and t.get("role") == "subtitle_dub" and t.get("from_track") == tid), None)
        if audio:
            audio.update(src=url, lang=lang, muted=False, hidden=False, dubbed=False, dub_src="", dub_lang=lang,
                         audio_rev=time.time_ns(), clips=[{"start": 0.0, "end": duration, "hidden": False}])
        else:
            audio = {"id": _new_track_id(job), "kind": "audio", "name": "目标语言语音", "hidden": False,
                     "muted": False, "src": url, "lang": lang, "role": "subtitle_dub", "from_track": tid,
                     "clips": [{"start": 0.0, "end": duration, "hidden": False}], "cues": [], "volume": 1.0,
                     "fade_in": 0.0, "fade_out": 0.0, "dubbed": False, "dub_src": "", "dub_lang": lang,
                     "audio_rev": time.time_ns()}
            tracks.append(audio)
        job["tracks"] = tracks
        job["dub_wav"] = str(dub)
        job["status"] = "dubbed"
        _save_jobs()
    _set_task_progress(jid, 100, "语音轨生成完成")
    return audio


@router.post("/api/jobs/{jid}/tracks/{tid}/synthesize")
def synthesize_subtitle_track(jid: str, tid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        track = _find_track(jobs[jid], tid)
        if not track or track.get("kind") != "subtitle":
            raise HTTPException(400, "仅字幕轨可生成语音")
        task_id = _begin_task_locked(jobs[jid], "subtitle_synthesize", tid)
    threading.Thread(target=_run_thread, args=(_step_synthesize_subtitle, jid, task_id, tid), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.post("/api/jobs/{jid}/config")
def config_job(jid: str, cfg: ConfigUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        if cfg.target_lang:
            if cfg.target_lang not in VOICE_MAP:
                raise HTTPException(400, f"暂不支持语种 {cfg.target_lang}，可选: {','.join(VOICE_MAP)}")
            jobs[jid]["target_lang"] = cfg.target_lang
        if cfg.default_gender:
            allowed = {"female", "female2", "male", "male2", "narration", "narration2"}
            if cfg.default_gender not in allowed:
                raise HTTPException(400, "默认声线无效")
            jobs[jid]["default_gender"] = cfg.default_gender
        _save_jobs()
    return {"ok": True, "target_lang": jobs[jid]["target_lang"],
            "default_gender": jobs[jid].get("default_gender", DEFAULT_GENDER)}


@router.post("/api/jobs/{jid}/clips")
def save_clips(jid: str, upd: ClipsUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        if upd.video_clips:
            # 规整：确保每段有 start/end/hidden 且数值合法；保留变换字段
            clean = []
            for c in upd.video_clips:
                try:
                    s = float(c.get("start", 0))
                    e = float(c.get("end", 0))
                except (TypeError, ValueError):
                    continue
                if e - s < 0.05:
                    continue
                crop = c.get("crop") or None
                if isinstance(crop, dict):
                    crop = {"mode": str(crop.get("mode", "none") or "none")}
                clean.append({
                    "start": s, "end": e, "hidden": bool(c.get("hidden", False)),
                    "rotate": int(c.get("rotate", 0) or 0) % 360,
                    "flip_h": bool(c.get("flip_h", False)),
                    "flip_v": bool(c.get("flip_v", False)),
                    "crop": crop,
                    "speed": float(c.get("speed", 1.0) or 1.0),
                })
            clean.sort(key=lambda x: x["start"])
            jobs[jid]["video_clips"] = clean
        jobs[jid]["dub_hidden"] = bool(upd.dub_hidden)
        _save_jobs()
    return {"ok": True, "video_clips": jobs[jid].get("video_clips"), "dub_hidden": jobs[jid]["dub_hidden"]}


@router.post("/api/jobs/{jid}/asr")
def asr_job(jid: str, model: str = ""):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        m = model or pipeline.WHISPER_MODEL
        task_id = _begin_task_locked(jobs[jid], "asr")
    threading.Thread(target=_run_thread, args=(_step_asr, jid, task_id, m), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.put("/api/jobs/{jid}/subtitles")
def save_subtitles(jid: str, upd: SegsUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        segs = [{"index": s.index, "start": s.start, "end": s.end, "zh": s.zh,
                 "hidden": bool(getattr(s, "hidden", False)),
                 "style": getattr(s, "style", None) or None}
                for s in upd.segments]
        segs.sort(key=lambda x: x["index"])
        jobs[jid]["refined_segments"] = segs
        # 合并相邻句时前端会同步发来 km_segments（与 refined 按 index 对齐），一并持久化
        if upd.km_segments:
            km = [{"index": int(k.get("index", i)), "start": float(k.get("start", 0)),
                   "end": float(k.get("end", 0)), "km": k.get("km", ""),
                   "zh": k.get("zh", ""), "gender": k.get("gender", "female")}
                  for i, k in enumerate(upd.km_segments)]
            km.sort(key=lambda x: x["index"])
            jobs[jid]["km_segments"] = km
        # 精修不降级已配音/已导出的状态（避免轮询把按钮误禁用、影响后续流程）
        cur = jobs[jid].get("status")
        if cur in (None, "uploaded", "asr_done"):
            jobs[jid]["status"] = "refined"
        _save_jobs()
    # 同步写中文 srt（含标记，供预览）
    jd = JOBS_DIR / jid
    pipeline.write_srt([(s["start"], s["end"], s["zh"]) for s in segs], jd / "zh_refined.srt")
    return {"ok": True, "count": len(segs)}


@router.post("/api/jobs/{jid}/dub")
def dub_job(jid: str, lang: str = ""):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "dub")
    threading.Thread(target=_run_thread, args=(_step_dub, jid, task_id, lang), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.post("/api/jobs/{jid}/remove_vocals")
def remove_vocals_job(jid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "remove_vocals")
    threading.Thread(target=_run_thread, args=(_step_remove_vocals, jid, task_id), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.post("/api/jobs/{jid}/extract_audio")
def extract_audio_job(jid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "extract_audio")
    threading.Thread(target=_run_thread, args=(_step_extract_audio, jid, task_id), daemon=True).start()
    return {"ok": True, "status": "running"}


@router.post("/api/jobs/{jid}/add_audio")
async def add_audio_job(jid: str, file: UploadFile = File(...)):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jd = JOBS_DIR / jid
        safe_name = _safe_filename(file.filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in AUDIO_EXTS:
            raise HTTPException(400, "仅支持音频文件")
        dest = jd / ("added_audio" + ext)
    _save_upload(file, dest)
    with LOCK:
        job = jobs[jid]
        restored = "dubbed" if job.get("km_segments") else ("refined" if job.get("refined_segments") else "asr_done")
    _set(jid, added_audio=str(dest), audio_source="added", status=restored)
    return {"ok": True, "audio_source": "added", "added_audio": str(dest)}


@router.post("/api/jobs/{jid}/toggle_mute")
def toggle_mute_job(jid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        m = not bool(jobs[jid].get("audio_muted", False))
        jobs[jid]["audio_muted"] = m
        _save_jobs()
    return {"ok": True, "audio_muted": m}


@router.post("/api/jobs/{jid}/audio_params")
def audio_params_job(jid: str, p: AudioParamsUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jobs[jid]["base_volume"] = max(0.0, min(4.0, float(p.base_volume)))
        jobs[jid]["dub_volume"] = max(0.0, min(4.0, float(p.dub_volume)))
        jobs[jid]["fade_in"] = max(0.0, min(30.0, float(p.fade_in)))
        jobs[jid]["fade_out"] = max(0.0, min(30.0, float(p.fade_out)))
        _save_jobs()
    return {"ok": True, "base_volume": jobs[jid]["base_volume"],
            "dub_volume": jobs[jid]["dub_volume"],
            "fade_in": jobs[jid]["fade_in"], "fade_out": jobs[jid]["fade_out"]}


@router.put("/api/jobs/{jid}/audio")
def save_audio_full(jid: str, p: AudioFullUpdate):
    # 撤销/重做回退用：一次性保存音频整体状态（禁音/音源/音量/淡入淡出）
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jobs[jid]["audio_muted"] = bool(p.audio_muted)
        jobs[jid]["audio_source"] = p.audio_source or "video"
        jobs[jid]["base_volume"] = max(0.0, min(4.0, float(p.base_volume)))
        jobs[jid]["dub_volume"] = max(0.0, min(4.0, float(p.dub_volume)))
        jobs[jid]["fade_in"] = max(0.0, min(30.0, float(p.fade_in)))
        jobs[jid]["fade_out"] = max(0.0, min(30.0, float(p.fade_out)))
        _save_jobs()
    return {"ok": True, "audio_muted": jobs[jid]["audio_muted"], "audio_source": jobs[jid]["audio_source"],
            "base_volume": jobs[jid]["base_volume"], "dub_volume": jobs[jid]["dub_volume"],
            "fade_in": jobs[jid]["fade_in"], "fade_out": jobs[jid]["fade_out"]}


@router.post("/api/jobs/{jid}/sub_shift")
def sub_shift_job(jid: str, s: SubShiftUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jobs[jid]["sub_shift"] = float(s.shift)
        _save_jobs()
    return {"ok": True, "sub_shift": jobs[jid]["sub_shift"]}


@router.post("/api/jobs/{jid}/retTS/{idx}")
async def retts(jid: str, idx: int):
    return {"url": await _synth_preview(jid, idx)}


@router.put("/api/jobs/{jid}/subtitle_style")
def save_subtitle_style(jid: str, st: SubStyleUpdate):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        jobs[jid]["subtitle_style"] = {
            "font_size": st.font_size, "color": st.color, "bg": st.bg,
            "bg_color": st.bg_color, "bg_opacity": st.bg_opacity,
            "bold": st.bold, "outline": st.outline, "position": st.position, "x": st.x, "y": st.y,
            "font": st.font or "",
            "box_width": st.box_width, "box_height": st.box_height,
        }
        _save_jobs()
    return {"ok": True, "subtitle_style": jobs[jid]["subtitle_style"]}


@router.put("/api/jobs/{jid}/name")
def rename_job(jid: str, body: dict = None):
    """保存项目名称（导出文件名 = 此名称）。"""
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        name = (body or {}).get("title", "") if body else ""
        name = (name or "").strip()[:180] or "未命名项目"
        jobs[jid]["title"] = name
        jobs[jid]["title_auto"] = False
        _save_jobs()
    return {"ok": True, "title": jobs[jid]["title"]}


@router.post("/api/jobs/{jid}/export")
def export_job(jid: str):
    with LOCK:
        if jid not in jobs:
            raise HTTPException(404, "job 不存在")
        task_id = _begin_task_locked(jobs[jid], "export")
    threading.Thread(target=_run_thread, args=(_step_export, jid, task_id), daemon=True).start()
    return {"ok": True, "status": "running"}


# FastAPI 会在 include_router 调用时复制当前路由，因此必须放在全部 @router 定义之后。
app.include_router(router)
