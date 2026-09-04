#!/bin/bash
# 下载一套常用字体到正式运行目录 fonts/，并写入 manifest.json（友好名字映射）。
# 同时安装到 ~/.fonts 让 ffmpeg 烧录时能用同名字体。
# 任一字体下载失败则跳过，不影响其余；字体目录为空时工作台仍可正常用系统默认字体。
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
DIR="$(cd -- "$SCRIPT_DIR/../drama_localizer/fonts" && pwd)"
mkdir -p "$DIR"
cd "$DIR" || exit 1

# 文件名 | 下载URL | 友好显示名（=字体内部 family，烧录与预览需一致）
FONTS=(
  "NotoSansKhmer.woff2|https://cdn.jsdelivr.net/npm/@fontsource/noto-sans-khmer@5/files/noto-sans-khmer-khmer-400-normal.woff2|Noto Sans Khmer"
  "NotoSansKhmerBold.woff2|https://cdn.jsdelivr.net/npm/@fontsource/noto-sans-khmer@5/files/noto-sans-khmer-khmer-700-normal.woff2|Noto Sans Khmer Bold"
  "NotoSansSC.woff2|https://cdn.jsdelivr.net/npm/@fontsource/noto-sans-sc@5/files/noto-sans-sc-chinese-simplified-400-normal.woff2|Noto Sans SC"
  "NotoSerifSC.woff2|https://cdn.jsdelivr.net/npm/@fontsource/noto-serif-sc@5/files/noto-serif-sc-chinese-simplified-400-normal.woff2|Noto Serif SC"
  "MaShanZheng.woff2|https://cdn.jsdelivr.net/npm/@fontsource/ma-shan-zheng@5/files/ma-shan-zheng-chinese-hong-kong-400-normal.woff2|Ma Shan Zheng"
  "ZhiMangXing.woff2|https://cdn.jsdelivr.net/npm/@fontsource/zhi-mang-xing@5/files/zhi-mang-xing-chinese-simplified-400-normal.woff2|Zhi Mang Xing"
  "ZCOOLKuaiLe.woff2|https://cdn.jsdelivr.net/npm/@fontsource/zcool-kuaile@5/files/zcool-kuaile-chinese-simplified-400-normal.woff2|ZCOOL KuaiLe"
  "LongCang.woff2|https://cdn.jsdelivr.net/npm/@fontsource/long-cang@5/files/long-cang-latin-400-normal.woff2|Long Cang"
  "Lobster.woff2|https://cdn.jsdelivr.net/npm/@fontsource/lobster@5/files/lobster-latin-400-normal.woff2|Lobster"
  "Oswald.woff2|https://cdn.jsdelivr.net/npm/@fontsource/oswald@5/files/oswald-latin-400-normal.woff2|Oswald"
  "Pacifico.woff2|https://cdn.jsdelivr.net/npm/@fontsource/pacifico@5/files/pacifico-latin-400-normal.woff2|Pacifico"
  "LiuJianMaoCao.woff2|https://cdn.jsdelivr.net/npm/@fontsource/liu-jian-mao-cao@5/files/liu-jian-mao-cao-latin-400-normal.woff2|Liu Jian Mao Cao"
)

rm -f manifest.json
printf "{" > manifest.json
first=1
for entry in "${FONTS[@]}"; do
  IFS='|' read -r fname url fam <<< "$entry"
  if curl -fL --retry 2 --max-time 90 -o "$fname" "$url" 2>/dev/null && [ -s "$fname" ]; then
    echo "OK   $fname -> $fam"
  else
    echo "SKIP $fname (下载失败)"
    rm -f "$fname"
    continue
  fi
  if [ $first -eq 1 ]; then first=0; else printf "," >> manifest.json; fi
  printf '\n  "%s": "%s"' "$fname" "$fam" >> manifest.json
done
printf '\n}\n' >> manifest.json

# 安装到 fontconfig，让 ffmpeg 烧录字幕时能用同名字体
mkdir -p ~/.fonts
for f in *.woff2; do cp -f "$f" ~/.fonts/ 2>/dev/null || true; done
fc-cache -f >/dev/null 2>&1 || true
echo "DONE: $(ls *.woff2 2>/dev/null | wc -l) 个字体已就绪，manifest.json 已生成"
