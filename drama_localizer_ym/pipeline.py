#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文短剧 -> 高棉语本地化 自动流水线 (drama_localizer)

流程:
  1. ASR  : faster-whisper 识别中文语音 -> 带时间戳 SRT (中文)
  2. 翻译 : LLM (OpenAI / DeepSeek 兼容接口) 中译高棉，保持时间戳与行数
  3. TTS  : 默认 edge-tts (微软 Edge 免费 km-KH-SreymomNeural, 免 key)；
            可切 Google Cloud TTS(需凭证)。逐句合成后严格对齐字幕窗口
            (配音过长则 atempo 加速塞满，避免重叠错轨 —— 参考 khmer_tts_strict.py)
  4. 合成 : ffmpeg 替换原音轨 + 封装(或烧录)高棉字幕

用法:
  python pipeline.py input.mp4                 # 输出 input.km.mp4
  python pipeline.py input.mp4 -o out.mp4      # 指定输出
  python pipeline.py input.mp4 --burn         # 把字幕烧录进画面
  python pipeline.py ./videos/                 # 批量处理目录下所有视频

配置: 复制 .env.example 为 .env 并填好 API key / 凭证路径
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# libass 的实际字形高度比浏览器 CSS 字号更小；该系数统一用于文字、换行和背景框。
ASS_FONT_SCALE = 1.75

# ---------- 配置 (全部来自 .env) ----------
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
SRC_LANG = os.getenv("SRC_LANG", "zh")
TGT_LANG = os.getenv("TGT_LANG", "km")
# TTS 提供方：edge(默认, 微软 Edge 免费 TTS, 免 key) | google(Google Cloud TTS, 需凭证)
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge").lower()
EDGE_VOICE = os.getenv("EDGE_VOICE", "km-KH-SreymomNeural")  # 微软免费高棉语神经语音
TTS_VOICE = os.getenv("TTS_VOICE", "km-KH-Standard-A")  # 仅 google 提供方使用
TTS_PITCH = float(os.getenv("TTS_PITCH", "0"))
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
BURN_SUBTITLE = os.getenv("BURN_SUBTITLE", "false").lower() == "true"
GLOSSARY_PATH = os.getenv("GLOSSARY_PATH", "")
LOG_MAX_BYTES = 1024 * 1024
LOG_PATH = Path(os.getenv("DRAMA_LOG_FILE", str(Path(__file__).resolve().parent / "workstation.log")))
_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    # 所有服务端、任务和前端回传诊断都走这里，记录带毫秒与时区的统一时间线。
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    line = f"{now} [drama-localizer] {msg}"
    # systemd 将标准输出追加到 workstation.log；达到 1MB 时先截断，避免无限增长。
    # 使用锁保证后台线程并发写日志时不会重复截断或交错检查。
    with _LOG_LOCK:
        try:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
                with LOG_PATH.open("w", encoding="utf-8"):
                    pass
        except OSError:
            pass
        print(line, flush=True)


# ---------- SRT 工具 ----------
def write_srt(segments, path) -> None:
    """segments: list of (start_sec, end_sec, text)"""

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(segments, 1):
            f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text.strip()}\n\n")
    log(f"写出字幕: {path}")


def _hex_to_ass(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB -> &HAABBGGRR（ASS 用 BGR 顺序，alpha 前两位）。"""
    h = (hex_color or "#FFFFFF").lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        h = "FFFFFF"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    # ASS alpha 与 CSS 相反：00=完全不透明，FF=完全透明。
    aa = max(0, min(255, int((1.0 - max(0.0, min(1.0, alpha))) * 255)))
    return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"


def wrap_subtitle_text(text: str, style: dict, width: int, height: int) -> str:
    """按前端字幕框宽度预先换行，避免 ASS 在 ``\\pos`` 定位时横向溢出。

    浏览器预览使用 ``word-break: break-word``；libass 对绝对定位字幕的自动换行
    不稳定，因此导出时必须写入显式换行。以字形宽度单位估算，保守一些以保证
    导出不会比预览更宽。返回普通换行，之后由 ``esc`` 转为 ASS 的 ``\\N``。
    """
    raw = str(text or "").replace("\\N", "\n")
    st = style or {}
    try:
        font_px = max(1.0, float(st.get("font_size", 28) or 28) * float(height) / 1080.0 * ASS_FONT_SCALE)
        box_px = max(font_px, min(float(width), float(st.get("box_width", 0.92) or 0.92) * float(width)))
    except (TypeError, ValueError):
        font_px, box_px = 28.0, float(width) * 0.92
    # CSS 背景有左右 0.45em padding；无背景也保留少量安全边距。
    usable = max(font_px, box_px - font_px * (0.9 if st.get("bg") else 0.20))
    limit = max(1.0, usable / (font_px * 0.82))

    def unit(ch):
        if ch.isspace():
            return 0.35
        if unicodedata.combining(ch) or unicodedata.category(ch).startswith("M"):
            return 0.0
        if "\u1780" <= ch <= "\u17ff":
            # 高棉文字一个可见字往往由多个 Unicode 码位组成；此前按 1.0
            # 逐码位估算过宽，导致浏览器仍是两行时导出被硬切成三行。
            # 这个系数按 Noto Sans Khmer 的实际字形宽度校准，组合附标仍由上面
            # 的 Mark 分支计作 0，不会被拆到下一行。
            return 0.65
        # 拉丁字符通常窄于中/高棉字符，其他文字按一个全角字估算。
        return 0.58 if ord(ch) < 0x2E80 and unicodedata.east_asian_width(ch) not in ("W", "F") else 1.0

    def reflow(max_units):
        lines = []
        for paragraph in raw.split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current, used = [], 0.0
            for ch in paragraph:
                size = unit(ch)
                # 高棉元音/声调等组合符必须跟在前一个基字后，不能被切到新行开头。
                is_mark = unicodedata.combining(ch) or unicodedata.category(ch).startswith("M")
                if current and used + size > max_units and not is_mark:
                    lines.append("".join(current).rstrip())
                    current, used = [], 0.0
                current.append(ch)
                used += size
            lines.append("".join(current).rstrip())
        return lines

    lines = reflow(limit)
    # 预览的字幕框由用户按两行效果调好。此前服务端用保守的 Unicode 宽度估算，
    # 会把同一段高棉语硬写成第三行；ASS 收到 ``\\N`` 后无法再自行回流。
    # 因此只要初排超过两行，就重新以两行预算排版。字号、框宽、框高均不改变。
    if len(lines) > 2:
        lines = reflow(limit * (len(lines) / 2.0))
    return "\n".join(lines)


def write_ass(segments, path, style: dict = None, width: int = 1920, height: int = 1080,
              styles: list = None, backgrounds: list = None) -> None:
    """生成带样式的 ASS 字幕（字体/颜色/背景/加粗/位置）。
    segments: list of (start_sec, end_sec, text)
    style : 全局默认样式（某行无逐行样式时生效）
    styles: 与 segments 等长的逐行样式列表；某行为 None 则回退全局 style
    backgrounds: 可选的 ASS 矢量圆角背景列表；与文字写入同一字幕流，避免每句
                 背景都创建一路全尺寸 SVG 视频而耗尽 ffmpeg 内存
    单条样式字段: font_size,color,bg,bg_color,bg_opacity,bold,position,x,y,box_width
    """
    base = style or {}
    # 字体族：浏览器预览用同名 web 字体；烧录时 ffmpeg 走 fontconfig，需 VM 已安装同名字体
    def safe_font(value):
        return re.sub(r"[^\w .()\-\u4e00-\u9fff]", "", str(value or ""), flags=re.UNICODE)[:120]

    # 高棉目标字幕必须使用包含 Khmer glyph 的字体；fontconfig 会为中文等其它字符自动回退。
    fn_base = safe_font(base.get("font")) or "Noto Sans Khmer"

    def _style_of(i):
        s = styles[i] if (styles and i < len(styles)) else None
        return s or base or {}

    fs = int(base.get("font_size", 28))
    color = _hex_to_ass(base.get("color", "#FFFFFF"), 1.0)
    bold = -1 if base.get("bold") else 0
    bg = base.get("bg", True)
    if bg:
        back = _hex_to_ass(base.get("bg_color", "#000000"), float(base.get("bg_opacity", 0.55)))
        border_style = 4          # 不透明边框框（背景盒）
        outline = 0
        outline_col = _hex_to_ass("#000000", 1.0)
    else:
        back = _hex_to_ass("#000000", 0.0)
        border_style = 1          # 仅描边
        outline = 2
        outline_col = _hex_to_ass("#000000", 1.0)
    # 预设对齐（未拖动时）
    align = {"top": 8, "middle": 5, "bottom": 2}.get(base.get("position", "bottom"), 2)

    def fmt(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60); cs = int(round((t - int(t)) * 100))
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    def esc(text):
        return (text or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N").strip()

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{fn_base},{fs},{color},&H000000FF,{outline_col},{back},{bold},0,0,0,"
        f"100,100,0,0,{border_style},{outline},0,{align},20,20,20,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # ASS 的 BorderStyle=4 只能生成直角盒。用 \p1 矢量路径绘制圆角矩形，既能
    # 保持预览中的圆角效果，也只占一个 subtitles 滤镜，不随字幕条数增加视频流。
    for bg_item in backgrounds or []:
        start = float(bg_item.get("start", 0) or 0)
        end = float(bg_item.get("end", start) or start)
        if end <= start:
            continue
        x = float(bg_item.get("x", 0) or 0)
        y = float(bg_item.get("y", 0) or 0)
        w = max(1.0, float(bg_item.get("width", 1) or 1))
        h = max(1.0, float(bg_item.get("height", 1) or 1))
        r = max(0.0, min(float(bg_item.get("radius", 0) or 0), w / 2.0, h / 2.0))
        left, top, right, bottom = x, y, x + w, y + h
        k = r * 0.55228475
        path_data = (
            f"m {left+r:.2f} {top:.2f} "
            f"l {right-r:.2f} {top:.2f} "
            f"b {right-r+k:.2f} {top:.2f} {right:.2f} {top+r-k:.2f} {right:.2f} {top+r:.2f} "
            f"l {right:.2f} {bottom-r:.2f} "
            f"b {right:.2f} {bottom-r+k:.2f} {right-r+k:.2f} {bottom:.2f} {right-r:.2f} {bottom:.2f} "
            f"l {left+r:.2f} {bottom:.2f} "
            f"b {left+r-k:.2f} {bottom:.2f} {left:.2f} {bottom-r+k:.2f} {left:.2f} {bottom-r:.2f} "
            f"l {left:.2f} {top+r:.2f} "
            f"b {left:.2f} {top+r-k:.2f} {left+r-k:.2f} {top:.2f} {left+r:.2f} {top:.2f}"
        )
        rgba = _hex_to_ass(str(bg_item.get("color", "#000000")),
                           float(bg_item.get("opacity", 0.55) or 0.55))
        aa, bgr = rgba[2:4], rgba[4:10]
        drawing = (f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c&H{bgr}&\\1a&H{aa}&}}"
                   f"{path_data}{{\\p0}}")
        lines.append(f"Dialogue: 0,{fmt(start)},{fmt(end)},Default,,0,0,0,,{drawing}")

    for i, (start, end, text) in enumerate(segments):
        st = _style_of(i)
        txt = esc(wrap_subtitle_text(text, st, width, height))
        # 前端字号以 1080 高画布为基准，ASS 使用实际输出 PlayRes。
        # 浏览器 CSS 与 libass 的字形高度不同；使用全局校准常量确保导出不会偏小。
        font_scale = (float(height) / 1080.0 * ASS_FONT_SCALE) if height else 1.0
        ov = []
        if st:
            # 背景由导出链路中的 SVG 独立绘制；不能再给文字塞空格，否则文字会
            # 相对预览横向漂移，也会让 ASS 自己的盒模型干扰用户设定的背景尺寸。
            ov.append(f"\\fs{max(1, int(round(float(st.get('font_size', fs)) * font_scale)))}")
            ov.append(f"\\c{_hex_to_ass(st.get('color', '#FFFFFF'), 1.0)}")
            # 使用明确字重而非布尔字重，让 libass/fontconfig 选择 700 字重；
            # 仅有常规字库时也会触发合成加粗。
            ov.append("\\b700" if st.get("bold") else "\\b0")
            ov.append("\\bord2" if st.get("outline") else "\\bord0")
            if st.get("font"):
                ov.append(f"\\fn{{{safe_font(st['font'])}}}")
            if st.get("bg"):
                ov.append(f"\\3c{_hex_to_ass(st.get('bg_color', '#000000'), 1.0)}")
                ov.append(f"\\4c{_hex_to_ass(st.get('bg_color', '#000000'), float(st.get('bg_opacity', 0.55)))}")
            if "x" in st and "y" in st:
                x = max(0.0, min(1.0, float(st["x"]))) * width
                y = max(0.0, min(1.0, float(st["y"]))) * height
                # 浏览器预览的字幕锚点是盒子中心；ASS 也必须用中心对齐，
                # 否则两行字幕会以底边定位而从背景框溢出。
                ov.append("\\an5")
                ov.append(f"\\pos({x:.0f},{y:.0f})")
        if ov:
            # ASS 覆盖样式必须位于大括号内，否则会被当作字幕正文显示。
            txt = "{" + "".join(ov) + "}" + txt
        bw = max(0.1, min(1.0, float(st.get("box_width", 0.92) or 0.92)))
        margin = max(0, int(round((1.0 - bw) * width / 2.0)))
        lines.append(f"Dialogue: 1,{fmt(start)},{fmt(end)},Default,,{margin},{margin},0,,{txt}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"写出 ASS 字幕(带样式): {path}")


# ---------- 0. 繁体 -> 简体 ----------
_CC = None  # 缓存 OpenCC 转换器，避免每句重复实例化

def _to_simplified(text):
    """繁体中文 -> 简体中文。识别片源(粤/港/台)常出繁体，统一转简体。
    优先 opencc(Python 重实现)，缺失则回退 hanziconv，再缺失原样返回。
    已简化的文本经 t2s 仍是自身（幂等），故"一直转简体"对简体片源安全。"""
    global _CC
    if not text:
        return text
    if _CC is None:
        try:
            from opencc import OpenCC
            _CC = OpenCC("t2s")
        except Exception:
            _CC = False
    if _CC is False:
        try:
            from hanziconv import HanziConv
            return HanziConv.toSimplified(text)
        except Exception:
            return text
    return _CC.convert(text)


# ---------- 1. ASR ----------
def _is_punctuation(ch: str) -> bool:
    """统一识别中英文及其它 Unicode 标点。"""
    return bool(ch) and unicodedata.category(ch).startswith("P")


def strip_subtitle_punctuation(text: str) -> str:
    """字幕显示文本不保留任何标点，只保留文字、数字和空白。"""
    return "".join(ch for ch in (text or "") if not _is_punctuation(ch)).strip()


def asr(video_path, model_size=WHISPER_MODEL, lang=SRC_LANG, progress_cb=None):
    """短窗口识别中文语音，并按语音段、标点和真实停顿生成字幕。"""
    from faster_whisper import WhisperModel

    log(f"加载 Whisper 模型 {model_size} (语言={lang}) ...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    vad = dict(threshold=0.35, neg_threshold=0.20,
               min_silence_duration_ms=350, speech_pad_ms=180)
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
                               check=True, capture_output=True, text=True, timeout=30)
        duration = max(0.0, float(probe.stdout.strip()))
    except Exception:
        duration = 0.0

    # 长文件整段解码会受远处上下文干扰（“婆”被移到数秒前就是这一类问题）。
    # 30 秒窗口、5 秒重叠；重叠区按窗口中心归属，既覆盖边界又不会重复回灌。
    starts = [0.0]
    if duration > 0:
        starts = []
        pos = 0.0
        while pos < duration:
            starts.append(pos)
            pos += 25.0
    records = []
    for wi, left in enumerate(starts):
        right = min(duration, left + 30.0) if duration > 0 else 0.0
        kwargs = {}
        if duration > 0:
            kwargs["clip_timestamps"] = f"{left:.3f},{right:.3f}"
        segments, _ = model.transcribe(
            video_path, language=lang, beam_size=5, word_timestamps=True,
            condition_on_previous_text=False, vad_filter=True,
            vad_parameters=vad, hallucination_silence_threshold=0.5, **kwargs)
        own_left = 0.0 if wi == 0 else left + 2.5
        own_right = float("inf") if wi == len(starts) - 1 else starts[wi + 1] + 2.5
        for seg in segments:
            midpoint = (float(seg.start) + float(seg.end)) / 2
            if own_left <= midpoint < own_right:
                records.append(seg)
        if progress_cb and duration > 0:
            progress_cb(right)
        if duration <= 0:
            break

    # 先把原生语音段变成词组。低置信度单字只有在前后都明显静音时才丢弃，
    # 防止误删连续对白中的“它”等正常单字。
    groups = []
    for seg in sorted(records, key=lambda s: (float(s.start), float(s.end))):
        raw_words = list(getattr(seg, "words", None) or [])
        seg_text = strip_subtitle_punctuation(_to_simplified(seg.text or ""))
        seg_span = max(0.0, float(seg.end) - float(seg.start))
        # 静音区的重复幻觉通常只有一两个字，却被拉长数秒甚至十几秒。
        if seg_span > 3.0 and len(seg_text) / seg_span < 1.0:
            log(f"ASR 丢弃低文字密度长段 {seg.start:.2f}-{seg.end:.2f} {seg_text!r}")
            continue
        if not raw_words:
            if seg_text:
                groups.append([{"start": float(seg.start), "end": float(seg.end), "text": seg_text}])
            continue
        current = []
        for i, word in enumerate(raw_words):
            start, end = float(word.start), float(word.end)
            raw = _to_simplified(str(word.word or "")).strip()
            clean = strip_subtitle_punctuation(raw)
            punct = any(_is_punctuation(ch) for ch in raw)
            probability = float(getattr(word, "probability", 1.0) or 0.0)
            prev_end = float(raw_words[i - 1].end) if i else float(seg.start)
            next_start = float(raw_words[i + 1].start) if i + 1 < len(raw_words) else float(seg.end)
            isolated = start - prev_end >= 0.80 and next_start - end >= 0.80
            if clean and len(clean) == 1 and probability < 0.40 and isolated:
                log(f"ASR 丢弃低置信度孤立字 {clean!r} {start:.2f}-{end:.2f} p={probability:.3f}")
                continue
            if current and start - current[-1]["end"] >= 0.30:
                groups.append(current); current = []
            if clean:
                current.append({"start": start, "end": end, "text": clean})
            if punct and current:
                groups.append(current); current = []
        if current:
            groups.append(current)

    # 第二层：若一组仍超过 11 字，从真实词边界里优先选择停顿较大且靠近中部的位置。
    final_groups = []
    def split_group(group):
        total = sum(len(t["text"]) for t in group)
        if total <= 11:
            final_groups.append(group); return
        if len(group) == 1:
            token = group[0]
            text = token["text"]
            cut = (len(text) + 1) // 2
            ratio = cut / len(text)
            mid = token["start"] + (token["end"] - token["start"]) * ratio
            split_group([{**token, "end": mid, "text": text[:cut]}])
            split_group([{**token, "start": mid, "text": text[cut:]}])
            return
        target = total / 2
        candidates, chars = [], 0
        for i in range(1, len(group)):
            chars += len(group[i - 1]["text"])
            gap = max(0.0, group[i]["start"] - group[i - 1]["end"])
            # 真实停顿优先，其次靠近中部；多字词边缘略优于拆在连续单字之间。
            word_edge = 0.10 if len(group[i - 1]["text"]) > 1 or len(group[i]["text"]) > 1 else 0.0
            score = gap * 20.0 + word_edge - abs(chars - target) / max(total, 1)
            candidates.append((score, i))
        # 分数相同时选靠前的边界，避免把“设计、花卷”等词从中间劈开。
        _, cut_at = max(candidates, key=lambda item: (item[0], -item[1]))
        split_group(group[:cut_at])
        split_group(group[cut_at:])
    for group in groups:
        split_group(group)

    out = [(g[0]["start"], g[-1]["end"], "".join(t["text"] for t in g))
           for g in final_groups if g]
    # Whisper 偶尔在窗口内部返回互相覆盖的重复短句；字幕时间轴必须单调且不重叠。
    normalized = []
    for cue in sorted(out, key=lambda item: (item[0], item[1])):
        if normalized and cue[0] < normalized[-1][1]:
            previous = normalized[-1]
            if previous[2] in cue[2]:
                normalized[-1] = cue
                continue
            if cue[2] in previous[2]:
                continue
            clipped = (previous[0], cue[0], previous[2])
            if clipped[1] > clipped[0] + 0.05:
                normalized[-1] = clipped
            else:
                normalized.pop()
        normalized.append(cue)
    out = normalized
    log(f"ASR 完成，共 {len(out)} 句（短窗口原生语音段+标点+停顿；超长块按真实词边界兜底）")
    return out


# ---------- 2. 翻译 ----------
def _build_prompt(cn_lines, glossary, tgt_lang_name="高棉语(柬埔寨语)"):
    gl = ""
    if glossary:
        gl = f"\n以下是必须统一使用的专有名词对照表（严格遵循）：\n{glossary}\n"
    lines_block = "\n".join(cn_lines)
    return (
        f"你是一名专业的中译{tgt_lang_name}字幕翻译。\n"
        f"任务：把下面每一行中文台词翻译成{tgt_lang_name}。\n"
        "严格要求：\n"
        "1. 逐行翻译，输出行数必须与输入完全一致、顺序一致；\n"
        "2. 每行只输出翻译结果，不要序号、不要解释、不要引号；\n"
        "3. 口语化，贴合短剧台词语气；\n"
        "4. 专有名词保持前后一致。" + gl +
        "\n\n[中文台词]\n" + lines_block +
        f"\n\n[{tgt_lang_name}翻译，逐行]"
    )


def _build_single_line_prompt(line, glossary, tgt_lang_name="高棉语(柬埔寨语)"):
    """逐句翻译使用不带序号/示例标签的短提示，降低模型复读提示词的概率。"""
    gl = f"\n术语表：{glossary}" if glossary else ""
    script_rule = "输出必须至少包含一个高棉文字字符。" if "高棉" in tgt_lang_name else ""
    return (
        f"把下方中文台词翻译成{tgt_lang_name}。\n"
        "只输出一条翻译后的台词正文。绝对不要输出序号、#、语言名称、说明、标签、引号或原文。\n"
        f"{script_rule}{gl}\n"
        "中文台词如下：\n" + line
    )


def _build_chunk_prompt(lines, glossary, tgt_lang_name="高棉语(柬埔寨语)"):
    """三句一组：JSON 输出可验证行数与顺序，失败再回退逐句。"""
    gl = f"\n术语表：{glossary}" if glossary else ""
    script_rule = "每个数组元素必须至少包含一个高棉文字字符。" if "高棉" in tgt_lang_name else ""
    source = json.dumps(list(lines), ensure_ascii=False)
    return (
        f"把 JSON 数组中的每一条中文台词翻译成{tgt_lang_name}。\n"
        f"只返回一个合法 JSON 字符串数组，长度必须恰好为 {len(lines)}，顺序必须与输入一致。\n"
        "数组每一项只能是译文正文；禁止序号、#、语言名称、解释、标签、原文和 Markdown 代码块。\n"
        f"{script_rule}{gl}\n"
        "中文台词 JSON：\n" + source
    )


def _clean_single_translation(content, tgt_lang_name):
    """提取单句结果；高棉语必须真含高棉文字，不能把提示词当字幕写入。"""
    raw = (content or "").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if "高棉" in tgt_lang_name:
        # 偶尔模型会加“14#”等前缀；只保留实际含高棉文字的那一行。
        line = next((x for x in lines if re.search(r"[\u1780-\u17ff]", x)), "")
        line = re.sub(r"^\s*(?:\d+\s*[#、.:：)）-]\s*)+", "", line)
        return line.strip()
    return lines[0] if lines else ""


def _parse_chunk_translation(content, expected_count, tgt_lang_name):
    """只接受完整 JSON 数组；不完整/污染输出由调用方回退逐句。"""
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or len(values) != expected_count:
        return None
    cleaned = [_clean_single_translation(str(value), tgt_lang_name) for value in values]
    return cleaned if all(cleaned) else None


def _translate_in_chunks(client, cn_lines, glossary, tgt_lang_name="高棉语(柬埔寨语)", progress_cb=None, chunk_size=3):
    """优先三句一请求；单组有任何不确定性就局部回退，不污染已完成字幕。"""
    out, total = [], max(len(cn_lines), 1)
    for start in range(0, len(cn_lines), chunk_size):
        chunk = cn_lines[start:start + chunk_size]
        translated = None
        last_error = None
        prompt = _build_chunk_prompt(chunk, glossary, tgt_lang_name)
        for attempt in range(3):
            try:
                log(f"LLM 三句请求 {start + 1}-{start + len(chunk)}/{len(cn_lines)}（第 {attempt + 1} 次）")
                response = client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                )
                translated = _parse_chunk_translation(response.choices[0].message.content, len(chunk), tgt_lang_name)
                if translated:
                    break
                last_error = RuntimeError("模型返回的 JSON 翻译格式或语种不合格")
            except Exception as ex:
                last_error = ex
            if attempt < 2:
                delay = (attempt + 1) * 2
                log(f"⚠️ LLM 三句请求暂时失败，将在 {delay}s 后重试: {type(last_error).__name__}")
                time.sleep(delay)
        if not translated:
            log(f"⚠️ 第 {start + 1}-{start + len(chunk)} 句三句翻译不合格，回退逐句: {last_error}")
            translated = _translate_per_line(client, chunk, glossary, tgt_lang_name)
        out.extend(translated)
        if progress_cb:
            progress_cb(min(100, len(out) * 100 / total))
    return out


def _translate_batch(client, cn_lines, glossary, tgt_lang_name="高棉语(柬埔寨语)", progress_cb=None):
    prompt = _build_prompt(cn_lines, glossary, tgt_lang_name)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception as ex:
        # 批量请求异常时立即切到逐句模式，让任务可见进度且避免整批永久阻塞。
        log(f"⚠️ 批量翻译请求失败，回退逐句翻译: {type(ex).__name__}: {ex}")
        return _translate_per_line(client, cn_lines, glossary, tgt_lang_name, progress_cb)
    out = resp.choices[0].message.content.strip()
    km_lines = [line.rstrip() for line in out.split("\n") if line.strip() != ""]
    if len(km_lines) != len(cn_lines):
        log(f"⚠️ 批量翻译行数不匹配(输入{len(cn_lines)}/输出{len(km_lines)})，回退逐句翻译")
        km_lines = _translate_per_line(client, cn_lines, glossary, tgt_lang_name, progress_cb)
    elif progress_cb:
        progress_cb(100)
    return km_lines


def _translate_per_line(client, cn_lines, glossary, tgt_lang_name="高棉语(柬埔寨语)", progress_cb=None):
    out = []
    total = max(len(cn_lines), 1)
    for i, line in enumerate(cn_lines):
        p = _build_single_line_prompt(line, glossary, tgt_lang_name)
        r = None
        last_error = None
        for attempt in range(3):
            try:
                log(f"LLM 逐句请求 {i + 1}/{len(cn_lines)}（第 {attempt + 1} 次）")
                r = client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[{"role": "user", "content": p}],
                    temperature=0.3,
                )
                translated = _clean_single_translation(r.choices[0].message.content, tgt_lang_name)
                if not translated:
                    raise RuntimeError(f"模型未返回{tgt_lang_name}译文")
                out.append(translated)
                r = None  # 已处理完毕；避免在循环外再次写入原始输出。
                break
            except Exception as ex:
                last_error = ex
                if attempt < 2:
                    delay = (attempt + 1) * 2
                    log(f"⚠️ LLM 逐句请求暂时失败，将在 {delay}s 后重试: {type(ex).__name__}")
                    time.sleep(delay)
        # 成功时已 append；失败三次才中止整个翻译任务，绝不写入提示词残片。
        if len(out) != i + 1:
            raise RuntimeError(f"第 {i + 1} 句翻译连续 3 次失败: {last_error}") from last_error
        if progress_cb:
            progress_cb((i + 1) * 100 / total)
    return out


def translate_lines(cn_lines, glossary="", tgt_lang_name="高棉语(柬埔寨语)", progress_cb=None):
    if not LLM_API_KEY:
        raise RuntimeError("缺少 LLM_API_KEY，请在 .env 配置")
    from openai import OpenAI

    # 防止上游 LLM 网络异常时任务永久停在进度条起点。
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=60.0, max_retries=0)
    # 每次三句：显著减少请求数；任一组不合格自动局部回退逐句。
    # 翻译轨仍遵守“字幕不显示标点”的统一规则。
    lines = _translate_in_chunks(client, cn_lines, glossary, tgt_lang_name, progress_cb, chunk_size=3)
    return [strip_subtitle_punctuation(line) for line in lines]


def _llm_copy_text(prompt: str, purpose: str) -> str:
    """调用当前配置的 OpenAI 兼容模型，生成/翻译社媒文案；不绑定任何厂商或模型名。"""
    if not LLM_API_KEY:
        raise RuntimeError("缺少 LLM_API_KEY，请在 .env 配置")
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=60.0, max_retries=0)
    last_error = None
    for attempt in range(3):
        try:
            log(f"LLM 文案{purpose}请求（第 {attempt + 1} 次）")
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7 if purpose == "生成" else 0.3,
            )
            text = str(response.choices[0].message.content or "").strip()
            if text:
                return text
            raise RuntimeError("模型未返回文案")
        except Exception as ex:
            last_error = ex
            if attempt < 2:
                time.sleep((attempt + 1) * 2)
    raise RuntimeError(f"文案{purpose}连续 3 次失败: {last_error}")


def generate_promo_copy(cn_lines, target_lang_name: str) -> dict:
    """顺序生成中文剧情钩子，再翻译为英语和当前目标语言，返回可复制的三语文案。"""
    source = "\n".join(str(x).strip() for x in cn_lines if str(x).strip())
    if not source:
        raise RuntimeError("没有中文字幕，无法生成剧情文案")
    # 控制上下文大小，避免长剧集的全部台词使文案请求缓慢或超过上游限制。
    source = source[:12000]
    zh_prompt = (
        "根据下面的中文字幕，写一条让人想点开观看的影视剧情简介。\n"
        "要求：约20个中文汉字描述剧情钩子；末尾追加2到4个相关 #标签；"
        "不得编造字幕中没有的关键人物或情节；只输出文案本身，不要标题、解释或引号。\n\n"
        f"中文字幕：\n{source}"
    )
    zh = _llm_copy_text(zh_prompt, "生成").replace("\n", " ").strip()
    if not zh:
        raise RuntimeError("中文剧情文案为空")

    def translate_copy(language: str) -> str:
        prompt = (
            f"把下面的中文影视社媒文案翻译为{language}。保留并翻译 #标签；"
            "保持简短、吸引点击；只输出译文，不要解释、标题或引号。\n\n"
            f"中文文案：{zh}"
        )
        return _llm_copy_text(prompt, f"翻译为{language}").replace("\n", " ").strip()

    en = translate_copy("英语")
    target = en if target_lang_name == "英语" else translate_copy(target_lang_name)
    return {"zh": zh, "en": en, "target": target, "target_lang": target_lang_name}


# ---------- 3. TTS ----------
def _atempo_chain(factor):
    """atempo 单段范围 [0.5, 2.0]，超出需链式叠加（参考 khmer_tts_strict.py 的踩坑修复）。"""
    parts = []
    f = float(factor)
    while f > 2.000001:
        parts.append("atempo=2.0")
        f /= 2.0
    parts.append("atempo=%.6f" % max(f, 0.5))
    return ",".join(parts)


def _probe_duration(path, ffprobe="ffprobe"):
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _fit_clip(in_path, out_wav, win_dur, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """严格对齐到字幕窗口（核心踩坑修复）：
    配音比窗口长 -> atempo 加速塞满；比窗口短 -> 保持原速不减速（避免变调失真）。"""
    dur = _probe_duration(in_path, ffprobe)
    if dur <= 0:
        raise RuntimeError(f"无法获取配音时长: {in_path}")
    sped = False
    if dur > win_dur and win_dur > 0:
        factor = dur / win_dur
        af = _atempo_chain(factor)
        if factor > 3.0:
            log(f"⚠️ 加速比过大 {factor:.2f}x，可能失真（#{os.path.basename(in_path)}）")
        cmd = [ffmpeg, "-y", "-i", in_path, "-af", af, "-ar", "44100", "-ac", "1", out_wav]
        sped = True
    else:
        cmd = [ffmpeg, "-y", "-i", in_path, "-ar", "44100", "-ac", "1", out_wav]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
    return out_wav, sped


async def _edge_synth_async(texts, voice, out_dir):
    import edge_tts

    paths = []
    for i, t in enumerate(texts):
        p = os.path.join(out_dir, f"{i:05d}.mp3")
        if os.path.exists(p) and os.path.getsize(p) > 100:
            paths.append(p)
            continue
        c = edge_tts.Communicate(t, voice)
        await asyncio.wait_for(c.save(p), timeout=120)
        paths.append(p)
    return paths


def tts_khmer(segments, voice=None):
    """逐句合成高棉语配音，并严格对齐到字幕窗口。返回 (segs_with_wav, tmp_dir)。"""
    tmp = tempfile.mkdtemp(prefix="tts_")
    texts = [t for (_, _, t) in segments]
    windows = [max(e - s, 0.0) for (s, e, _) in segments]

    if TTS_PROVIDER == "google":
        if not GOOGLE_CREDENTIALS or not os.path.exists(GOOGLE_CREDENTIALS):
            raise RuntimeError("TTS_PROVIDER=google 但缺少 GOOGLE_APPLICATION_CREDENTIALS")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDENTIALS
        from google.cloud import texttospeech

        client = texttospeech.TextToSpeechClient()
        vconf = texttospeech.VoiceSelectionParams(language_code="km-KH", name=voice or TTS_VOICE)
        aconf = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            pitch=TTS_PITCH, speaking_rate=TTS_SPEED,
        )
        raw_paths = []
        for i, t in enumerate(texts):
            resp = client.synthesize_speech(
                input=texttospeech.SynthesisInput(text=t), voice=vconf, audio_config=aconf,
                timeout=120)
            p = os.path.join(tmp, f"{i:05d}.wav")
            with open(p, "wb") as f:
                f.write(resp.audio_content)
            raw_paths.append(p)
            if (i + 1) % 20 == 0 or (i + 1) == len(texts):
                log(f"TTS {i + 1}/{len(texts)}")
    else:  # edge-tts（默认，微软 Edge 免费 TTS，免 key）
        voice = voice or EDGE_VOICE
        mp3_dir = os.path.join(tmp, "mp3")
        os.makedirs(mp3_dir, exist_ok=True)
        log(f"edge-tts 合成 (voice={voice}) ...")
        raw_paths = asyncio.run(_edge_synth_async(texts, voice, mp3_dir))

    # 严格对齐到窗口（防止高棉语配音比原窗口长导致重叠错轨）
    results = []
    n_speed = 0
    for i, (start, end, _) in enumerate(segments):
        wav = os.path.join(tmp, f"{i:05d}.fit.wav")
        _, sped = _fit_clip(raw_paths[i], wav, windows[i])
        if sped:
            n_speed += 1
        results.append((start, end, wav))
    log(f"TTS 完成 {len(results)} 句，其中 {n_speed} 句需加速对齐窗口")
    return results, tmp


# ---------- 3b. 通用单句 edge-tts 合成（按性别选 voice 用） ----------
_TTS_CALL_LOCK = threading.Lock()
_TTS_LAST_CALL = 0.0

async def _edge_synth_one(text, voice, out_path, rate="+0%", pitch="+0Hz"):
    import edge_tts
    c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await asyncio.wait_for(c.save(out_path), timeout=120)


def synth_edge(text, voice, out_path, rate="+0%", pitch="+0Hz", attempts=6):
    """合成单句到 mp3；Edge 短暂不返回音频时自动重试，并清理不完整文件。"""
    global _TTS_LAST_CALL
    last = None
    # Edge 免费端点会对短时间内的连续请求返回“无音频”。串行节流比盲目重试稳定，
    # 同一进程内的多个任务也不能同时轰击端点。
    with _TTS_CALL_LOCK:
        for attempt in range(1, max(1, attempts) + 1):
            try:
                # 实测该端点在约 6 个连续请求后会返回空音频，2.2 秒间隔避开频率窗口。
                wait = max(0.0, 2.2 - (time.monotonic() - _TTS_LAST_CALL))
                if wait:
                    time.sleep(wait)
                _TTS_LAST_CALL = time.monotonic()
                asyncio.run(_edge_synth_one(text, voice, out_path, rate=rate, pitch=pitch))
                if not os.path.exists(out_path) or os.path.getsize(out_path) <= 100:
                    raise RuntimeError("TTS 返回了空音频")
                return
            except Exception as ex:
                last = ex
                try:
                    if os.path.exists(out_path):
                        os.unlink(out_path)
                except OSError:
                    pass
                if attempt < attempts:
                    delay = min(30, 3 * (2 ** (attempt - 1)))
                    log(f"Edge TTS 暂时失败，{delay}s 后重试 {attempt + 1}/{attempts}（voice={voice}）")
                    time.sleep(delay)
    raise last


# ---------- 4. 合成 ----------
def build_dub_audio(segments_wav, total_duration, tmp):
    """把每句 wav 按 start 延迟叠加，生成完整配音轨 wav"""
    if not segments_wav:
        raise RuntimeError("没有可合成的配音片段")
    inputs = []
    filters = []
    for i, (start, _end, wav) in enumerate(segments_wav):
        inputs += ["-i", wav]
        delay_ms = int(start * 1000)
        filters.append(f"[{i}]adelay={delay_ms}|{delay_ms}[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(segments_wav)))
    # 每句通常位于不同时间窗口。amix 默认 normalize=1 会按总输入数衰减：
    # 例如 60 句会把每句都压低约 35 dB，文件有波形但听起来近似静音。
    # 禁用该归一化以保留单句原始响度，并用 limiter 防止少量重叠句削波。
    filter_complex = (
        ";".join(filters)
        + f";{mix}amix=inputs={len(segments_wav)}:duration=longest:normalize=0,"
          "alimiter=limit=0.95[aout]"
    )
    out_wav = os.path.join(tmp, "dub.wav")
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]", "-t", str(total_duration), out_wav,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    return out_wav


def mux(video_path, dub_wav, km_srt, output_path, burn=BURN_SUBTITLE):
    # dub_wav 为 None => 整体静音：只输出视频（无音轨）
    if dub_wav is None:
        cmd = ["ffmpeg", "-y", "-i", video_path]
        # 静音也烧录字幕：有样式烧 ASS、无样式烧 SRT（避免字幕丢失；无音轨无法软字幕封装）
        if km_srt and Path(km_srt).exists() and (burn or str(km_srt).lower().endswith((".srt", ".ass"))):
            cmd += ["-vf", f"subtitles={km_srt}"]
        cmd += ["-map", "0:v", "-c:v", "libx264", "-an", "-shortest", output_path]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        except subprocess.CalledProcessError as ex:
            err = ex.stderr.decode(errors="replace") if ex.stderr else ""
            raise RuntimeError(f"mux(静音) 失败(rc={ex.returncode}): {err[-2000:]}")
        log(f"合成完成(静音无音轨): {output_path}")
        return
    if burn:
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", dub_wav,
            "-vf", f"subtitles={km_srt}",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", output_path,
        ]
    else:
        # 注意：MP4 容器不支持直接 copy SRT，软字幕必须转码为 mov_text
        # （MKV 则可用 copy，但统一用 mov_text 兼容性最好）
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", dub_wav, "-i", km_srt,
            "-map", "0:v", "-map", "1:a", "-map", "2:s",
            "-c", "copy", "-c:a", "aac", "-c:s", "mov_text",
            "-metadata:s:a:0", "language=km",
            "-metadata:s:2", "language=km",
            "-shortest", output_path,
        ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    except subprocess.CalledProcessError as ex:
        err = ex.stderr.decode(errors="replace") if ex.stderr else ""
        raise RuntimeError(f"mux 失败(rc={ex.returncode}): {err[-2000:]}")
    log(f"合成完成: {output_path}")


# ---------- 主流程 ----------
def run(video_path, output_path=None, burn=BURN_SUBTITLE):
    video_path = str(video_path)
    base = Path(video_path).stem
    work = Path(tempfile.mkdtemp(prefix="drama_"))
    cn_srt = work / f"{base}.zh.srt"
    km_srt = work / f"{base}.km.srt"
    if output_path is None:
        output_path = str(Path(video_path).with_name(f"{base}.km.mp4"))

    glossary = ""
    if GLOSSARY_PATH and os.path.exists(GLOSSARY_PATH):
        glossary = Path(GLOSSARY_PATH).read_text(encoding="utf-8")

    # 1. ASR
    cn_segs = asr(video_path)
    if not cn_segs:
        raise RuntimeError("ASR 未识别到任何台词")
    write_srt(cn_segs, cn_srt)

    # 2. 翻译
    cn_lines = [t for (_, _, t) in cn_segs]
    km_lines = translate_lines(cn_lines, glossary)
    km_segs = [(s, e, km_lines[i]) for i, (s, e, _) in enumerate(cn_segs)]
    write_srt(km_segs, km_srt)

    # 3. TTS
    tts_segs, tts_tmp = tts_khmer(km_segs)

    # 4. 合成
    duration = cn_segs[-1][1]
    dub_wav = build_dub_audio(tts_segs, duration, tts_tmp)
    mux(video_path, dub_wav, str(km_srt), output_path, burn)

    log("✅ 全部完成 -> " + output_path)
    return output_path


def main():
    ap = argparse.ArgumentParser(description="中文短剧→高棉语本地化流水线")
    ap.add_argument("video", help="输入视频路径，或目录(批量处理)")
    ap.add_argument("-o", "--output", help="输出视频路径(默认同目录 .km.mp4)")
    ap.add_argument("--burn", action="store_true", help="烧录字幕到画面(默认封装软字幕)")
    args = ap.parse_args()

    if os.path.isdir(args.video):
        count = 0
        for f in sorted(Path(args.video).iterdir()):
            if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
                log(f"=== 处理 {f.name} ===")
                run(str(f), burn=args.burn)
                count += 1
        log(f"批量处理完成，共 {count} 个文件")
    else:
        run(args.video, args.output, args.burn)


if __name__ == "__main__":
    main()
