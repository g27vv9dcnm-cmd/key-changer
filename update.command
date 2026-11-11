#!/bin/sh
# Mac 用: YouTube が読み込めなくなったときにダブルクリックして yt-dlp を更新します
cd "$(dirname "$0")" || exit 1
sh ./start.sh --update
echo ""
echo "このウィンドウは閉じてかまいません。"
