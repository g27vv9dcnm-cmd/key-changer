"""起動用スクリプト(標準ライブラリのみ)。

1. .venv(仮想環境)が無ければ作る
2. 必要なパッケージをインストール(requirements.txt が変わったときだけ)
3. app.py を起動してブラウザを開く

  python launcher.py           起動
  python launcher.py --update  yt-dlp を最新版に更新
"""
import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"


def say(msg=""):
    print(msg, flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def pip(*args):
    subprocess.check_call([str(venv_python()), "-m", "pip", "--disable-pip-version-check", *args])


class VenvError(Exception):
    pass


def pip_works() -> bool:
    try:
        return subprocess.run([str(venv_python()), "-m", "pip", "--version"],
                              capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_venv():
    # STAMP はセットアップが最後まで成功したときだけ書かれる。
    # 途中で失敗した・ウィンドウを閉じた .venv は作り直す。
    if not venv_python().exists() or (not STAMP.exists() and not pip_works()):
        say("初回セットアップ中です(数分かかることがあります)…")
        try:
            venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
        except (OSError, subprocess.CalledProcessError) as e:
            raise VenvError(str(e)) from None
        if not pip_works():
            raise VenvError("pip がありません")
    digest = hashlib.sha256(REQ.read_bytes()).hexdigest()
    if not STAMP.exists() or STAMP.read_text().strip() != digest:
        say("必要な部品をインストールしています…")
        pip("install", "-q", "--upgrade", "pip")
        pip("install", "-q", "-r", str(REQ))
        STAMP.write_text(digest)


def find_ffmpeg():
    for keg in ("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "/usr/local/opt/ffmpeg-full/bin/ffmpeg"):
        if os.path.isfile(keg):
            return keg
    return shutil.which("ffmpeg")


def check_tools():
    if not find_ffmpeg():
        say("")
        say("[注意] ffmpeg が見つかりません。音声の変換に必要です。")
        say("  Windows: winget install --id Gyan.FFmpeg -e")
        say("  Mac    : brew install ffmpeg-full")
        say("  インストール後、このウィンドウを閉じてもう一度起動してください。")
        say("")
    if not shutil.which("deno"):
        say("[参考] deno が見つかりません。YouTube の読み込みに必要になることがあります(README 参照)。")


def main():
    if sys.version_info < (3, 10):
        say("Python 3.10 以上が必要です。今の Python: " + sys.version.split()[0])
        say("https://www.python.org/downloads/ から新しい Python をインストールしてください。")
        sys.exit(1)
    os.chdir(ROOT)
    try:
        ensure_venv()
        if "--update" in sys.argv:
            say("yt-dlp を最新版に更新しています…")
            pip("install", "-q", "--upgrade", "yt-dlp[default]")
            out = subprocess.run([str(venv_python()), "-m", "yt_dlp", "--version"],
                                 capture_output=True, text=True)
            say("更新しました。yt-dlp のバージョン: " + out.stdout.strip())
            return
    except VenvError:
        say("")
        say("[エラー] Python の仮想環境(.venv)を作れませんでした。")
        if sys.platform.startswith("linux"):
            say("  Ubuntu / Debian では次を実行してから、もう一度起動してください:")
            say("    sudo apt install python3-venv")
        else:
            say("  Python を入れ直してから、もう一度起動してください(README 参照)。")
        sys.exit(1)
    except subprocess.CalledProcessError:
        say("")
        say("[エラー] 部品のインストールに失敗しました。インターネット接続を確認して、もう一度起動してください。")
        say("  何度やっても失敗する場合は、このフォルダの中の .venv フォルダを削除してから起動してください。")
        sys.exit(1)
    check_tools()
    args = [str(venv_python()), str(ROOT / "app.py"), "--open"]
    try:
        sys.exit(subprocess.call(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
