# 🎬 DramaLocalizer — AI Film & TV Localization Workbench

<p align="center">
  <a href="README_zh-CN.md"><img src="https://img.shields.io/badge/文档-中文-blue?style=for-the-badge" alt="中文文档"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/Docs-English-blue?style=for-the-badge" alt="English Docs"></a>
</p>

> Turn raw video into a dubbed, subtitled, social-ready localization package — entirely on **your own server**. Upload a clip and DramaLocalizer runs speech recognition, human-in-the-loop subtitle editing, LLM translation, neural TTS dubbing, vocal isolation and FFmpeg composition end-to-end.

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux%20(Ubuntu%20server)-blue" alt="Platform">
  <img src="https://img.shields.io/badge/backend-FastAPI%2FPython-009688" alt="Backend">
  <img src="https://img.shields.io/badge/ASR-faster--whisper-orange" alt="ASR">
  <img src="https://img.shields.io/badge/TTS-Edge%20TTS-0a7" alt="TTS">
  <img src="https://img.shields.io/badge/license-Internal-red" alt="License">
  <img src="https://img.shields.io/badge/version-v20260904015-important" alt="Version">
</p>

---

## 🌟 Why DramaLocalizer?

- **🎯 One pipeline, zero glue code** — upload → recognize → translate → dub → isolate → compose → download, all from a single web UI.
- **🏠 Self-hosted & private** — every heavy model (Whisper, Demucs) runs locally; only the translation and TTS calls leave your network. Your media never touches a third party.
- **✍️ Human-in-the-loop** — recognized subtitles are editable before translation, so you keep full control over timing and wording.
- **🪄 Auto promo copy** — after a successful compose, an LLM writes a Chinese plot hook and hashtags, then translates them into English and the target language, stored per project as `promo_copy`.
- **🚀 Production-ready** — ships with a one-shot `install.sh`, a systemd unit, and a `deploy_d.py` that does rsync + restart + health-check + rollback.

```
            ┌─────────────── video ───────────────┐
            │                                       │
            ▼                                       │
      audio separation ──▶ Whisper ASR ──▶ subtitle edit (you)
                                                  │
                                                  ▼
   download ◀── FFmpeg compose (subs + image) ◀── timeline edit
        │                                        ▲
        │                                        │
        └─ promo_copy ◀─ LLM translate ◀─ Edge TTS dub ◀─ Demucs (vocal removal)
```

## ✨ Features

| Feature | Description |
| --- | --- |
| 🎙️ Speech recognition | Local **faster-whisper** `large-v3-turbo` produces timestamped Chinese subtitles. |
| ✍️ Subtitle editor | Web editor for human refinement of recognized text and timing. |
| 🌐 LLM translation | Alibaba Bailian OpenAI-compatible API (default `deepseek-v4-flash`); retries sentence-by-sentence on failure. |
| 🔊 Neural TTS dubbing | **Edge TTS** renders the target language per sentence; voice chosen by language + gender / narration tag. |
| 🎚️ Vocal isolation | Local **Demucs** `htdemucs` removes vocals and keeps the accompaniment. |
| 🎞️ Composition | **FFmpeg** (libass) bakes in subtitles and images and outputs the final video. |
| 🪄 Promo copy | Auto-generated plot hooks + `#hashtags` in Chinese, English and the target language. |
| 🖥️ Web UI | Single-file SPA (`static/index.html`), polls task progress. |
| 📦 Deploy & ops | `install.sh` (systemd + fonts + autostart) and `deploy_d.py` (rsync + health-check + rollback). |

## 📸 Screenshots

<p align="center">
  <img src="屏幕截图1.png" width="720" alt="主界面"><br>
  <sub>Main workbench</sub>
</p>
<p align="center">
  <img src="屏幕截图2.png" width="720" alt="项目库"><br>
  <sub>Project library</sub>
</p>

## 🧱 Tech Stack

| Layer | Choice |
| --- | --- |
| Backend | **FastAPI** (Python 3.14 verified) |
| Speech recognition | **faster-whisper** `large-v3-turbo` |
| Translation | Alibaba Bailian OpenAI-compatible API (`deepseek-v4-flash`) |
| Text-to-speech | **Microsoft Edge TTS** |
| Vocal removal | **Demucs** `htdemucs` |
| Composition | **FFmpeg** with `libass` subtitles |
| Frontend | Single-file SPA (`static/index.html`) |
| Deploy | `rsync` + `systemd` + SSH (`paramiko`) |
| Fonts | Noto Sans Khmer (and others) via fontconfig |

## 📂 Project Structure

```
DramaLocalizer/
├── install.sh              # one-shot installer: systemd unit + fonts + autostart
├── 说明.md                 # full architecture / deploy / migration guide (Chinese)
├── README.md               # this file (English)
├── README_zh-CN.md         # Chinese README
├── 屏幕截图1.png / 屏幕截图2.png
└── drama_localizer_ym/     # application source + model cache + docs
    ├── workstation.py      # FastAPI backend: routes, task state machine, project & file mgmt
    ├── pipeline.py         # localization engine
    ├── static/index.html   # frontend SPA
    ├── deploy_d.py         # remote deploy (rsync + systemd + health check + rollback)
    ├── run.sh              # launcher (uvicorn + pinned model cache path)
    ├── drama_localizer.service  # systemd unit example
    ├── fetch_fonts.sh      # fallback font downloader
    ├── requirements.txt / requirements-dev.txt / pyproject.toml
    ├── .env.example        # configuration sample
    ├── AUDIT.md / DELIVERY.md   # audit & delivery notes
    └── fonts/              # (git-ignored) deployable fonts
```

> Runtime artifacts (`venv/`, `models/`, `output/`, `workstation.log`, the real `.env`) are **not** committed — see `.gitignore`.

## ⚙️ How It Works

```
video ──▶ audio separation ──▶ Whisper ASR ──▶ subtitle edit (human)
                                                  │
                                                  ▼
download ◀── FFmpeg compose (subs+image) ◀── timeline edit ◀── Demucs (vocal removal)
   │                                               ▲
   │                                               │
   └─ promo_copy ◀── LLM translate ◀── Edge TTS dub ┘
```

**Engineering highlights**

- **Threaded heavy tasks.** Transcription, translation, TTS, Demucs and FFmpeg run in background threads; the SPA polls `/d/api` for progress so the UI never blocks.
- **Serialized per project.** Batch operations on a project library run one job at a time — no hammering the models or external services concurrently.
- **Pinned model cache.** `run.sh` points the model cache at `drama_localizer_ym/models/`, so migrations don't depend on `~/.cache`.
- **Health-checked deploys.** `deploy_d.py` uploads source, validates remotely, keeps a rollback backup, restarts the service and probes `/d/api/version` before declaring success.

## 📥 Installation

### Option A — One-shot installer (recommended)

On a fresh Ubuntu 24.04/26.04 server:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg curl fontconfig \
  ca-certificates build-essential libsndfile1 rsync

git clone https://github.com/Juzer7113/DramaLocalizer.git
cd DramaLocalizer
sudo bash install.sh          # creates the systemd unit, installs fonts, enables boot
```

`install.sh` derives the run dir, model dir, log path and fonts from the current directory, then enables the service to start on boot. If your run user differs from the directory owner, run `sudo CUTLY_USER=<user> bash install.sh`.

### Option B — Manual / from source

See [说明.md](说明.md) for the full walkthrough (server requirements, swap, venv, `.env`, fontconfig registration, systemd hardening and a 10-point acceptance checklist). The condensed steps:

```bash
# 1. system deps (above) + a "juzer" user + /www/wwwroot/aidj/drama_localizer*
# 2. copy source (excluding venv/models/.env/output)
sudo rsync -a --delete \
  --exclude models --exclude .git --exclude .env --exclude venv \
  --exclude output --exclude backups --exclude workstation.log \
  /path/to/DramaLocalizer/ /www/wwwroot/aidj/drama_localizer/
# 3. python venv + pip install -r requirements.txt
# 4. cp .env.example .env  and fill in keys
# 5. register fonts, create drama_localizer.service, systemctl enable --now
```

## 🕹️ Usage

1. Open `http://<server-ip>:8000/d` in a browser.
2. Enter the `WORKSTATION_API_KEY` from your `.env`.
3. **Upload** a video.
4. **Edit** the recognized (Whisper) subtitles — fix timing and wording.
5. Choose a **target language**, **translate** (LLM) and **generate TTS dubbing**.
6. **Remove vocals** (Demucs) and **edit the timeline**, then **compose** with FFmpeg.
7. **Download** the finished video; copy the auto-generated `promo_copy` (plot hook + hashtags) from the project or library view.

## ⚙️ Configuration

Copy `.env.example` to `.env` and edit. Key variables:

| Variable | Meaning | Example |
| --- | --- | --- |
| `WORKSTATION_API_KEY` | Access key for the workbench web UI | `set-a-secret-key` |
| `WHISPER_MODEL` | Local ASR model | `large-v3-turbo` |
| `LLM_API_KEY` | Key for the translation API | `sk-...` |
| `LLM_BASE_URL` | OpenAI-compatible endpoint | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | Translation model | `deepseek-v4-flash` |
| `TTS_PROVIDER` | TTS backend | `edge` |
| `SRC_LANG` | Source language | `zh` |
| `TGT_LANG` | Pipeline default target language | `km` |
| `TARGET_LANG` | Workbench default target language | `km` |

`TGT_LANG` is the pipeline's default; `TARGET_LANG` is the workbench's default for new projects. They usually match, but either can be overridden per project in the UI.

## 🚀 Deployment

After editing source, bump `APP_VER` in `static/index.html` and `workstation.py`, then:

```bash
export DRAMA_HOST='your-server-ip'
export DRAMA_SSH_PASSWORD='server-password'
export WORKSTATION_API_KEY="$(cat WORKSTATION_ACCESS_KEY.txt)"
python deploy_d.py
```

`deploy_d.py` uploads the source, validates remotely, keeps a rollback backup under `backups/`, restarts the systemd service and probes `/d/api/version`. After a deploy, hard-refresh the browser (`Ctrl+Shift+R`).

## 🛠️ Troubleshooting

| Symptom | Fix |
| --- | --- |
| Page won't open / 502 | Check `systemctl status drama_localizer`; ensure TCP 8000 is allowed (`sudo ufw allow 8000/tcp` if UFW is on); verify the cloud security group. |
| Subtitles show no Khmer glyphs | `sudo -u juzer fc-match 'Noto Sans Khmer'` — should resolve; otherwise reinstall fonts from `fonts/`. |
| Whisper is slow or OOM-killed | Ensure ≥ 8 GB RAM + 4 GB swap; verify with `swapon --show`. |
| Translation fails | Check `LLM_API_KEY` / `LLM_BASE_URL` are reachable; review `workstation.log`. |
| TTS produces no audio | Edge TTS needs outbound internet egress; confirm the server can reach it. |
| `subtitles` filter missing | `ffmpeg -hide_banner -filters \| grep subtitles` must list it; reinstall ffmpeg with libass. |

Runtime logs: `/www/wwwroot/aidj/drama_localizer/workstation.log` (auto-truncated at 1 MB).

## 📝 Changelog

### v20260904015 — Initial public source release
- FastAPI backend (`workstation.py`) with task state machine, project & file management, and single-file SPA frontend (`static/index.html`).
- Localization pipeline: faster-whisper ASR → human subtitle edit → LLM translation → Edge TTS dubbing → Demucs vocal removal → FFmpeg composition.
- Auto `promo_copy` generation (plot hooks + hashtags) in Chinese, English and target language.
- `install.sh` (systemd + fonts + autostart) and `deploy_d.py` (rsync + health-check + rollback).
- `.env.example`, `drama_localizer.service`, `fetch_fonts.sh`, `requirements.txt`, `pyproject.toml`.

## 📜 License

Internal use. This repository is published for source management and self-hosting; **no open-source license is granted** and redistribution is not permitted. Keep `.env`, `models/`, `output/` and `workstation.log` private.

## ☕ Like it?

If DramaLocalizer saves you time, give it a ⭐, open a PR, or share it with someone localizing video. Feedback and bug reports are always welcome.
