# Cutly · AI 辅助影视本地化工作台

基于 **FastAPI + faster-whisper + Edge TTS + Demucs + FFmpeg** 的一站式影视本地化工作台。上传视频后自动完成：

```
上传 → 音频分离 → Whisper 识别字幕 → 人工精修 → LLM 翻译
     → TTS 配音 → Demucs 去人声 → 时间轴编辑 → FFmpeg 视频合成 → 下载
```

视频合成成功后会调用配置的 LLM 生成中文剧情钩子与多语言推广文案，保存到项目 `promo_copy`。

## 目录结构

| 路径 | 说明 |
|---|---|
| `workstation.py` | 后端（FastAPI，任务状态机、项目管理、文件管理） |
| `pipeline.py` | 本地化引擎 |
| `static/` | 前端单文件 SPA |
| `deploy_d.py` | 远程部署脚本（rsync + systemd + 健康检查） |
| `install.sh` | 单机/迁移安装（生成 systemd 服务、安装字体、开机启动） |
| `drama_localizer.service` | systemd 单元示例 |
| `说明.md` | 完整架构、部署与迁移文档 |

## 核心模型与服务

- **faster-whisper** `large-v3-turbo`：本地中文语音识别与时间戳字幕
- **阿里云百炼** OpenAI 兼容接口（默认 `deepseek-v4-flash`）：字幕翻译
- **Edge TTS**：目标语言逐句配音
- **Demucs** `htdemucs`：去人声保留伴奏
- **FFmpeg**：转码、混音、字幕叠加与最终合成

## 快速部署

最小系统依赖：`python3-venv`、`ffmpeg`、`fontconfig`、`libsndfile1`。

完整步骤（全新服务器、迁移、systemd、验收清单）见 [说明.md](说明.md)。

```bash
# 修改源码后发布（需配置 DRAMA_HOST / WORKSTATION_API_KEY）
export DRAMA_SSH_PASSWORD='服务器密码'
export DRAMA_HOST='172.16.1.24'
python deploy_d.py
```

## 截图

![主界面](%E5%B1%8F%E5%B9%95%E6%88%AA%E5%9B%BE1.png)

![项目库](%E5%B1%8F%E5%B9%95%E6%88%AA%E5%9B%BE2.png)

## 许可证

内部使用。
