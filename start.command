#!/bin/sh
# Mac 用: このファイルをダブルクリックすると起動します
cd "$(dirname "$0")" || exit 1
sh ./start.sh
echo ""
echo "終了しました。このウィンドウは閉じてかまいません。"
