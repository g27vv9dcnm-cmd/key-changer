#!/bin/sh
# キー変更アプリを起動します (Mac / Linux)
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python が見つかりません。README の手順で Python をインストールしてください。"
  exit 1
fi
exec "$PY" launcher.py "$@"
