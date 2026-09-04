# CDTV 短剧本地化工作台 (drama_localizer) 交付说明

> 交付版本：`v20260828013`（前端 `APP_VER` 与后端 `APP_VER` 一致）
> 交付时间：2026-08-28
> 用途：中文短剧 → 多语种（默认高棉语 km，可切 th/en/vi/…）本地化 Web 工作台，含「识别→精修→配音→去人声→导出」人在环工作流。

---

## 1. 源码路径

### 1.1 本地开发目录（本机）
```
C:\Users\jiyan\WorkBuddy\2026-08-26-13-30-03\drama_localizer\
```
这是你本机唯一需要维护的源码根目录，所有改动都在这里改，再 `deploy_d.py` 推到服务器。

### 1.2 生产部署目录（VM）
```
/www/wwwroot/aidj/drama_localizer/        # VM 172.16.1.24 (用户 juzer)
```
线上访问地址：`http://172.16.1.24:8000/d`（所有工作台路由带 `/d` 前缀）。

---

## 2. 文件清单与职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `workstation.py` | 1853 | **后端核心**：FastAPI 服务、全部 `/d/api/*` 路由、任务状态机、文件管理、后台任务（分离/去人声）管控 |
| `pipeline.py` | 581 | **本地化引擎**：Whisper ASR、LLM 翻译、edge-tts 合成、时长严格对齐、字幕 SRT/ASS 写入、ffmpeg 合成导出 |
| `static/index.html` | 1713 | **前端单文件 SPA**：内联全部 HTML/CSS/JS，无构建步骤，直接由后端以 `/d/` 静态托管 |
| `run.sh` | 10 | systemd 启动包装：加载 `.env` 后拉起 uvicorn（生产用） |
| `drama_localizer.service` | 19 | systemd 单元文件（开机自启、崩溃重启、`Restart=always`） |
| `deploy_d.py` | 65 | **部署脚本**：SFTP 上传 5 个文件 → 校验导入 → 拉字体 → 重启服务 → 健康检查 |
| `requirements.txt` | 9 | Python 依赖：faster-whisper / openai / edge-tts / google-cloud-texttospeech / python-dotenv / fastapi / uvicorn / python-multipart / demucs |
| `.env.example` | 46 | 全部环境变量样例（TTS/LLM/Whisper/语言对/字幕烧录/术语表） |
| `fetch_fonts.sh` | — | 首次部署下载常用字体到 `fonts/`（失败不阻断启动） |
| `output/jobs/jobs.json` | — | **运行时任务库**（内存 + 落盘），每个 job 一个子目录 `output/jobs/{jid}/` |
| `e2e_test.py` / `e2e_ws*.py` / `check.py` / `probe.py` / `_smoke_edge.py` / `redeploy*.py` | — | 验证/冒烟/回归脚本（开发辅助，不参与生产） |

⚠️ **铁律**：改前端 `static/index.html` 或后端 `workstation.py` 任一处实质性改动，必须同步把两处 `APP_VER` 升号（格式 `v` + 日期 + 3位序号，如 `v20260828013`）；否则你看不到版本变化、会以为没改。本项目**无自动热刷新**，靠 `verBadge` + `no-store` 头提示，交付后务必 **Ctrl+Shift+R 硬刷新**。

---

## 3. 架构总览

```
浏览器 (static/index.html 单文件 SPA)
        │  HTTP  /d/*   (所有工作台路由统一 /d 前缀)
        ▼
FastAPI (workstation.py)
   ├─ /d/            → 返回 static/index.html
   ├─ /d/api/*       → 30 个 OpenAPI 业务操作 + 版本/字体/登录等隐藏路由
   ├─ /d/files/*     → StaticFiles 托管 output/jobs（视频/字幕/草稿/预览）
   ├─ /d/fonts/*     → StaticFiles 托管 fonts/（用户自丢 .ttf/.otf/.woff）
   └─ 调用 pipeline.py（同步函数，重活用 threading 后台任务跑，前端轮询）
        │
        ▼
pipeline.py → whisper / openai兼容LLM / edge-tts / ffmpeg / demucs
```

- **任务状态机**：`uploaded → asr_done → refined → dubbed → vocals_removed → exported`
- **进程模型**：主进程 uvicorn 单进程；分离原声(`separate`)、去人声(`remove_vocals`) 等重算用 `threading.Thread` 后台跑，前端每 1.5s 轮询 `GET /d/api/jobs/{jid}` 拿进度。
- **并发安全**：全局 `LOCK`（threading.Lock）保护 job 字典；`jobs.json` 每次关键状态变更落盘。
- **访问控制**：任务 API 和素材文件使用 `WORKSTATION_API_KEY` 登录，成功后签发 12 小时 HttpOnly Cookie。
- **任务持久化**：`jobs.json` 使用临时文件 + `fsync` + 原子替换，并保留 `jobs.json.bak`。

---

## 4. 关键 API 路由表（全部带 `/d` 前缀）

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/d/api/version` | 返回 `APP_VER`（前端 verBadge 比对用） |
| GET | `/d/api/fonts` | 列出 `fonts/` 下可用字体 |
| POST | `/d/api/jobs/upload` | 上传视频，建任务，落 `source.mp4` |
| GET | `/d/api/jobs` | 任务列表（**注意**：此处 `ensure_tracks` 会按 `source` 补齐轨道，见 §7） |
| GET | `/d/api/jobs/{jid}` | 单任务详情 + 归一化轨道 |
| GET/PUT | `/d/api/jobs/{jid}/tracks` | 读/写时间轴轨道（视频/音频/字幕/图片） |
| DELETE | `/d/api/jobs/{jid}/media/{mid}` | 删素材（连带删文件 + 派生整轨） |
| POST | `/d/api/jobs/{jid}/tracks/{tid}/separate` | 分离原声 → 派生音频轨 |
| POST | `/d/api/jobs/{jid}/task/abort` | **中止指定轨的后台任务**（删轨前调用，防竞态复活） |
| POST | `/d/api/jobs/{jid}/tracks/{tid}/vocals` | 去人声（demucs） |
| POST | `/d/api/jobs/{jid}/tracks/{tid}/asr` | 识别字幕 |
| POST | `/d/api/jobs/{jid}/tracks/{tid}/translate` | 翻译 |
| POST | `/d/api/jobs/{jid}/tracks/{tid}/dub` | 单轨配音 |
| POST | `/d/api/jobs/{jid}/clips` | 写剪辑片段（分割/偏移/删除片段） |
| POST | `/d/api/jobs/{jid}/export` | 合成导出最终成片 |
| POST | `/d/api/jobs/{jid}/sub_shift` | 字幕整体时间偏移 |
| POST | `/d/api/jobs/{jid}/subtitle_style` | 字幕样式（颜色/字体/位置） |
| … | 其余 16 个 | 添加音频/静音切换/音量/名称/配置/预览等 |

---

## 5. Job 数据结构（节选，存于 `output/jobs/jobs.json`）

```jsonc
{
  "id": "uuid",
  "status": "dubbed",                       // 状态机当前态
  "source": "/abs/path/source.mp4",         // 本机源视频
  "source_file": "/d/files/{id}/source.mp4",// 前端访问 URL
  "target_lang": "km",
  "cn_segments": [ {index,start,end,zh} ],  // ASR 结果
  "refined_segments": [ ... ],              // 人工精修后
  "km_segments": [ {index,start,end,zh,km,gender} ], // 翻译+配音后
  "tracks": [                               // 时间轴多轨
    {"id":"t_vid","kind":"video","src":"/d/files/{id}/source.mp4","clips":[...]},
    {"id":"t_aud","kind":"audio","src":"...","from_track":"t_vid"},  // 分离派生
    {"id":"t_sub","kind":"subtitle","cues":[...]}
  ],
  "draft": "/d/files/{id}/draft.mp4",
  "final": "/d/files/{id}/final.mp4"
}
```

---

## 6. 部署与运维

### 6.1 一键部署（改完本地代码后）
```bash
cd C:\Users\jiyan\WorkBuddy\2026-08-26-13-30-03\drama_localizer
python deploy_d.py
```
部署前需在环境中设置 `WORKSTATION_API_KEY`（测试阶段至少 8 位，正式环境建议 32 位以上）以及 SSH Key；若暂未配置 Key 登录，可临时通过 `DRAMA_SSH_PASSWORD` 注入密码。脚本不再保存明文凭据。
脚本动作：SFTP 上传 `workstation.py / pipeline.py / static/index.html / fetch_fonts.sh / run.sh` → 远端 `import workstation` 校验 → 拉字体 → `systemctl restart drama_localizer` → 健康检查 `/d/ /d/api/jobs /d/api/fonts /d/api/version` 全 200。
> 部署目标 VM `172.16.1.24`，账号 `juzer`，见 `deploy_d.py` 顶部 `HOST/USER/PW`。

### 6.2 生产启动方式（开机自启）
- `drama_localizer.service` 已由 systemd 托管：`WantedBy=multi-user.target`，`Restart=always`，崩溃自动拉起。
- 手动重启：`sudo systemctl restart drama_localizer`；看日志：`tail -f /www/wwwroot/aidj/drama_localizer/workstation.log`。

### 6.3 配置（`.env`，生产在 `/www/wwwroot/aidj/drama_localizer/.env`）
- `TTS_PROVIDER=edge`（默认免 key）；可切 `google`（需 `GOOGLE_APPLICATION_CREDENTIALS`）
- `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（当前阿里云百炼 deepseek-v4-flash）
- `WHISPER_MODEL=medium`（中文更准，首次自动下载）
- `SRC_LANG=zh` `TARGET_LANG=km`（前端可随时切 th/en/vi/lo/my/ja/ko/fr/es）
- `BURN_SUBTITLE=false`（false=可开关软字幕，true=烧录进画面）
- 可选 `GLOSSARY_PATH` 专有名词术语表 `中文=高棉语`

---

## 7. 本次交付重点修复（删除功能，v20260828006 → v20260828012）

这一版历经 7 次迭代，彻底解决了「时间轴删除」相关的竞态与误删。根因链与修复点：

| 版本 | 问题 | 修复 |
|---|---|---|
| v20260828006 | 误删媒体区小菜单删键（你只让删时间轴标签删键） | 还原媒体区 `🗑 删除素材`；新增后端 `DELETE /api/jobs/{jid}/media/{mid}`（删素材+文件+派生整轨） |
| v20260828007 | 时间轴片段浮动工具条 `clip-toolbar` 无删除键 | 三处（视频/音频/字幕图片）各加 `🗑 删除整轨` 按钮 |
| v20260828008 | 删视频冒音频、删音频冒视频（分离后台在跑时删轨复活） | 孤儿音频清理；`deleteMediaFromTimeline` 先 `stopPoll()`；按钮 `stopPropagation`；新增 `删除此片段`(`deleteClip`) |
| v20260828009 | 删片段后仍能播/拖；删唯一片段未删整轨 | `activeVideoSegments()`+`seekToValidPlayhead()` 预览跳过被删区间；`deleteClip` 删空轨→删整轨 |
| v20260828010 | 删片段仍冒音频（分离同步跑、删片段路径未堵） | 分离/去人声改**可中止**后台任务；新增 `POST /api/jobs/{jid}/task/abort`；删前先 abort |
| v20260828011 | 误判前端 `bindTracks` 空数组（`if(j.tracks && j.tracks.length)`） | 改为 `if(Array.isArray(j.tracks))`（保留但非主因） |
| v20260828012 | **真凶**：后端 `ensure_tracks` 守卫 `if job.get("tracks"):` 把空数组 `[]` 判 falsy，每次 `GET /api/jobs` 都重建已删轨道 | 守卫改为 `if job.get("tracks") is not None`；Playwright 真实浏览器复现，删除后前后端 tracks 均 1→0 清空 |

**最终删除语义（现网一致）**：
- 时间轴标签删键：**已按你要求删除**（不在左侧标签）。
- 媒体区素材小菜单：`🗑 删除素材` 保留。
- 时间轴片段浮动工具条 `clip-toolbar`：含 `删除此片段`（单 clip）+ `🗑 删除整轨`（整轨）。
- 删整轨：先 `abort` 该轨后台任务 → 删素材文件 → 删派生轨（删视频连带删其分离音频；删音频不动视频 `muted_src_audio`）。
- 删片段：删空该轨则自动降级为删整轨；预览自动跳过被删/隐藏区间。

---

## 8. 前端关键模块（`static/index.html` 内联 JS，无构建）

| 函数/区块 | 作用 |
|---|---|
| `APP_VER` (L434) | 版本常量，与后端一致，硬刷新后更新 verBadge |
| `bindTracks(j)` | 把后端 tracks 绑到前端 `state.tracks`（`Array.isArray` 判空） |
| `deleteMediaFromTimeline(id)` | 删整轨主流程（stopPoll → abort → DELETE） |
| `deleteClip(tid,kind,i)` | 删单片段，空轨则调删整轨 |
| `clip-toolbar` | 时间轴片段浮动工具条（分割/删除此片段/删除整轨） |
| `activeVideoSegments()` / `seekToValidPlayhead()` | 预览跳过删除/隐藏区间 |
| 撤销/重做 (v20260827001+) | ↶↷ 按钮 + Ctrl+Z / Ctrl+Shift+Z；快照覆盖 segs/videoClips/subStyle/subShift |
| 缩放 (v20260827001+) | `pxPerSec()` zoom=0 时 fit 整条时间轴，最小档不横向滚动 |

---

## 9. 二次开发 & 本地运行

1. 本地起服务（需要 Python venv + 依赖）：
   ```bash
   cd C:\Users\jiyan\WorkBuddy\2026-08-26-13-30-03\drama_localizer
   python -m venv venv && venv\Scripts\pip install -r requirements.txt
   venv\Scripts\python -m uvicorn workstation:app --host 0.0.0.0 --port 8000
   # 浏览器开 http://localhost:8000/d
   ```
2. 改前端：直接编辑 `static/index.html` 内联 JS/CSS，升 `APP_VER`，硬刷新即可（无需构建）。
3. 改后端：编辑 `workstation.py` / `pipeline.py`，升 `APP_VER`，跑 `deploy_d.py` 上 VM。
4. 新增语言：在 `workstation.py` 的 `VOICE_MAP` / `LANG_NAME` 加映射，前端 `TARGET_LANG` 选项同步。
5. 字体：把 `.ttf/.otf/.woff(2)` 丢进 `fonts/`，`/d/api/fonts` 自动出现。

---

## 10. 交付清单 & 验收

✅ 源码根目录：`C:\Users\jiyan\WorkBuddy\2026-08-26-13-30-03\drama_localizer\`
✅ 目标生产：VM `172.16.1.24:8000/d`，版本 `v20260828013`
✅ 健康检查：`/d/` `/d/api/jobs` `/d/api/fonts` `/d/api/version` 全 200
✅ 删除功能竞态已根除（Playwright 端到端复现通过，tracks 1→0）
✅ 铁律遵守：只删你点名处，未扩大化；媒体区删键、clip-toolbar 删键均保留

**你这边只需做**：生产环境 **Ctrl+Shift+R 硬刷新** 到 `v20260828013`，首次访问输入交付的工作台访问密钥，再验证核心工作流。
