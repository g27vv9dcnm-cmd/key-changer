"""キー変更アプリ(ローカル専用 Web アプリ)

起動:  python app.py --open
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import audio

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ID_RE = re.compile(r"^[0-9a-f]{20}$")
JOB_RE = re.compile(r"^[0-9a-f]{32}$")


def _prefetch_span() -> int:
    """環境変数 KEYCHANGER_PREFETCH: 先回りして変換しておく範囲(0 = しない、1 = 隣だけ。既定は 2 つ隣まで)。"""
    try:
        n = int(os.environ.get("KEYCHANGER_PREFETCH", "").strip() or 2)
    except ValueError:
        n = 2
    return max(0, min(n, 3))


@dataclass
class Settings:
    cache_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("KEYCHANGER_CACHE")
        or Path(tempfile.gettempdir()) / "key-changer-cache"))
    max_duration: float = float(os.environ.get("KEYCHANGER_MAX_MINUTES", "20")) * 60
    max_filesize: int = int(os.environ.get("KEYCHANGER_MAX_MB", "200")) * 1024 * 1024
    max_cache_bytes: int = int(os.environ.get("KEYCHANGER_CACHE_MB", "2000")) * 1024 * 1024
    max_cache_age: float = 7 * 24 * 3600
    max_download_seconds: float = float(os.environ.get("KEYCHANGER_MAX_DOWNLOAD_MINUTES", "15")) * 60
    part_max_age: float = 3600  # これより古い作業中ファイル(.part)は消す(変換のタイムアウトは 900 秒)
    engine: str | None = None  # None = 自動(rubberband があれば使う)
    jobs: int | None = None    # 並列変換に使うプロセス数。None = 自動(CPU のコア数)、1 = 並列にしない
    # 聴いているキーの近く(±この数まで)を先回りして変換しておく。0 = 先回りしない
    # (0 のときは、保存用の MP3 を前もって作っておくこともしない)
    prefetch: int = _prefetch_span()
    prefetch_long: float = 600  # これより長い音源は、先回りを隣(±1)までにする(時間もディスクも使うので)


settings = Settings()


# ---------------------------------------------------------------- エラー
class UserError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


MSG_FFMPEG = ("ffmpeg が見つかりません。README の手順で ffmpeg をインストールしてから、"
              "アプリを起動し直してください。")


# ---------------------------------------------------------------- キャッシュ
def sources_dir() -> Path:
    return settings.cache_dir / "sources"


def renders_dir() -> Path:
    return settings.cache_dir / "renders"


def check_id(source_id: str) -> str:
    if not isinstance(source_id, str) or not ID_RE.fullmatch(source_id):
        raise UserError("音声の ID が正しくありません。もう一度読み込んでください。", 400)
    return source_id


def source_path(source_id: str) -> Path | None:
    d = sources_dir() / check_id(source_id)
    if not d.is_dir():
        return None
    for p in d.iterdir():
        if p.name.startswith("source.") and not p.name.endswith(".part"):
            return p
    return None


def read_meta(source_id: str) -> dict | None:
    p = sources_dir() / check_id(source_id) / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_meta(source_id: str, meta: dict) -> None:
    """meta.json を書き直す(読んでいる途中のものに中途半端な内容を見せないよう、置き換える)。"""
    p = sources_dir() / source_id / "meta.json"
    tmp = p.with_name(f"meta.{uuid.uuid4().hex}.part")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def render_path(source_id: str, semitones: int) -> Path:
    """キーを変えた音声(再生用の WAV)の置き場所。"""
    check_id(source_id)
    audio.validate_semitones(semitones)
    return renders_dir() / f"{source_id}_{semitones:+d}.wav"


def mp3_path(source_id: str, semitones: int) -> Path:
    """保存(ダウンロード)用の MP3 の置き場所。"""
    check_id(source_id)
    audio.validate_semitones(semitones)
    return renders_dir() / f"{source_id}_{semitones:+d}.mp3"


# ブラウザの <audio> にそのまま渡して再生できる音源。拡張子 → (ffmpeg での入れ物の呼び名, 音声の形式, 種類)
# 読み込んだ音源がこれに当てはまれば、原曲キーは作り直さずにそのまま再生する(読み込みが速くなる)。
# 少しでも怪しいもの(MP3 は可変ビットレートだと再生位置がずれることがある、など)は入れていない。
# 当てはまらない音源は、これまでどおり変換したもの(WAV)を再生する。
_DIRECT: dict[str, tuple[str, frozenset[str], str]] = {
    ".m4a": ("mp4", frozenset({"aac"}), "audio/mp4"),
    ".mp4": ("mp4", frozenset({"aac"}), "audio/mp4"),
    ".webm": ("webm", frozenset({"opus", "vorbis"}), "audio/webm"),
    ".ogg": ("ogg", frozenset({"opus", "vorbis"}), "audio/ogg"),
    ".oga": ("ogg", frozenset({"opus", "vorbis"}), "audio/ogg"),
    ".opus": ("ogg", frozenset({"opus"}), "audio/ogg"),
    ".wav": ("wav", frozenset({"pcm_s16le"}), "audio/wav"),
    ".flac": ("flac", frozenset({"flac"}), "audio/flac"),
}


def direct_media_type(src: Path, info: dict) -> str | None:
    """音源をそのままブラウザで再生できるなら、その種類(Content-Type)を返す。info は audio.probe の結果。"""
    rule = _DIRECT.get(src.suffix.lower())
    if rule is None:
        return None
    container, codecs, media_type = rule
    if container not in (info.get("format") or "").split(","):
        return None  # 拡張子と中身が合っていない
    if info.get("codec") not in codecs or info.get("audio_streams") != 1 or info.get("video"):
        return None
    if info.get("codec") == "aac" and info.get("profile") not in ("LC", "HE-AAC", "HE-AACv2"):
        return None
    # 5.1ch などは、ブラウザと ffmpeg でステレオへの混ぜ方が違う(キーを変えたときと聞こえ方が変わる)
    if info.get("layout") not in ("mono", "stereo", "1 channels", "2 channels"):
        return None
    # 長さが分からない・途中の時刻から始まる音声は、再生位置がずれるのでそのままは使わない
    if not info.get("duration") or abs(info.get("start") or 0.0) > 0.1:
        return None
    return media_type


def direct_type(source_id: str) -> str | None:
    """この音源をそのままブラウザで再生できるなら、その種類を返す(できないなら None)。"""
    meta = read_meta(source_id)
    src = source_path(source_id)
    if meta is None or src is None:
        return None
    if "direct" not in meta:
        # 前のバージョンで読み込んだ音源。調べて、次からのために覚えておく
        try:
            meta["direct"] = direct_media_type(src, audio.probe(src))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        try:
            write_meta(source_id, meta)
        except OSError:
            pass  # 覚えられなくても、次にまた調べるだけ
    rule = _DIRECT.get(src.suffix.lower())
    return rule[2] if rule and meta.get("direct") == rule[2] else None


def _touch(p: Path) -> None:
    try:
        os.utime(p, None)
    except OSError:
        pass


def cleanup_cache(startup: bool = False, protect: Iterable[Path] = ()) -> None:
    """古いファイルを消し、合計サイズが上限を超えていれば古い順に消す。

    startup=True(起動時)は、前回の起動で途中になった並列変換の作業フォルダも消す。
    protect に渡したもの(いまから使う音源のフォルダや変換結果)は消さない。
    """
    protect = set(protect)
    root = settings.cache_dir
    root.mkdir(parents=True, exist_ok=True)
    sources_dir().mkdir(exist_ok=True)
    renders_dir().mkdir(exist_ok=True)
    now = time.time()
    entries: list[tuple[float, int, Path]] = []  # (mtime, size, path)
    for d in sources_dir().iterdir():
        try:
            if not d.is_dir():
                continue
            if d.name.startswith(".tmp-"):
                # ダウンロード中の作業フォルダ。古いもの(中断の残骸)だけ消す
                if now - d.stat().st_mtime > settings.part_max_age:
                    remove_path(d)
                continue
            files = list(d.iterdir())
            if not files:
                remove_path(d)
                continue
            mtime = max(f.stat().st_mtime for f in files)
            size = sum(f.stat().st_size for f in files)
        except OSError:
            continue
        entries.append((mtime, size, d))
    for f in renders_dir().iterdir():
        try:
            st = f.stat()
        except OSError:
            continue
        if f.name.endswith(".part"):
            # 変換中の一時ファイル・作業フォルダ。変換中のものは消さず、古い残骸だけ消す。
            # 作業フォルダは大きい(数百 MB)ので、起動時はすぐ消す(前回の途中のものしか無いはず)
            if now - st.st_mtime > settings.part_max_age or (startup and f.is_dir()):
                remove_path(f)
            continue
        entries.append((st.st_mtime, st.st_size, f))

    keep = []
    for mtime, size, p in entries:
        if now - mtime > settings.max_cache_age and p not in protect:
            remove_path(p)
        else:
            keep.append((mtime, size, p))
    keep.sort()
    total = sum(s for _, s, _ in keep)
    for _, size, p in keep:  # 古い順
        if total <= settings.max_cache_bytes:
            break
        if p not in protect:
            remove_path(p)
            total -= size


_cleanup_lock = threading.Lock()


def tidy_cache(*protect: Path) -> None:
    """キャッシュの掃除を、失敗しても気にせずに行う(ほかで掃除中なら何もしない)。"""
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        cleanup_cache(protect=protect)
    except OSError:
        pass
    finally:
        _cleanup_lock.release()


def remove_path(p: Path) -> None:
    """1 つ消せなくても(Windows で使用中など)残りの掃除は続ける。"""
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- ダウンロード
def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise UserError("URL を入力してください。")
    if len(url) > 2048:
        raise UserError("URL が長すぎます。")
    try:
        u = urlparse(url)
    except ValueError:
        raise UserError("URL の形式が正しくありません。") from None
    if u.scheme not in ("http", "https") or not u.netloc or not u.hostname:
        raise UserError("URL の形式が正しくありません。http:// または https:// で始まる URL を入力してください。")
    return url


def fmt_minutes(sec: float) -> str:
    return f"{int(sec // 60)}分{int(sec % 60):02d}秒"


MSG_JS_RUNTIME = ("YouTube の読み込みに必要な JavaScript 実行環境 (deno) が見つからないか、yt-dlp が古い可能性があります。"
                  "README の「deno のインストール」と「yt-dlp の更新」を試してください。")


def _translate_ytdlp_error(msg: str, warnings: list[str] | None = None) -> str:
    m = msg.lower()
    w = " ".join(warnings or []).lower()
    js_hint = any(k in w for k in ("javascript runtime", "js runtime", "deno", "n challenge",
                                   "nsig", "signature"))
    if "unsupported url" in m:
        return "この URL には対応していません。動画・音声のページの URL か、音声ファイルの URL を入力してください。"
    if "private" in m or "sign in" in m or "login" in m or "members-only" in m:
        return "この動画はログインが必要か非公開のため、読み込めません。"
    if ("javascript" in m or "js runtime" in m or "deno" in m or "n challenge" in m
            or "signature" in m or "requested format is not available" in m or js_hint):
        return MSG_JS_RUNTIME
    if "copyright" in m or "not available" in m or "unavailable" in m:
        return "この動画は利用できません(削除・地域制限などの可能性があります)。"
    if "http error 403" in m or "http error 429" in m:
        return "サイトにアクセスを拒否されました。しばらく待つか、yt-dlp を更新してから再度お試しください(README 参照)。"
    if "max-filesize" in m or "larger than max" in m or "file is larger" in m:
        return f"ファイルが大きすぎます(上限 {settings.max_filesize // 1024 // 1024}MB)。"
    if "name or service not known" in m or "getaddrinfo" in m or "timed out" in m or "connection" in m:
        return "サイトに接続できませんでした。URL とインターネット接続を確認してください。"
    return "ダウンロードに失敗しました。URL を確認するか、yt-dlp を更新してから再度お試しください(README 参照)。"


def _check_duration(duration: float | None) -> None:
    if duration and duration > settings.max_duration:
        raise UserError(
            f"長すぎます({fmt_minutes(duration)})。{int(settings.max_duration // 60)}分以内の音声にしてください。")


# 音声だけを取る。5.1ch などのサラウンド版は容量が 3 倍ほどあり(その分ダウンロードが遅い)、
# どのみちステレオに直して使うので、ステレオ版があればそちらを選ぶ。
AUDIO_FORMAT = "bestaudio[audio_channels<=?2]/bestaudio/best"
FRAGMENT_THREADS = 4  # 細切れで配信される音声(HLS など)を、いくつ並行してダウンロードするか


class _YdlLogger:
    """yt-dlp の警告を集める(エラーを分かりやすい言葉に直すときの手がかりにする)。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): self.warnings.append(str(msg))
    def error(self, msg): pass


def _ydl_opts(logger: _YdlLogger, work: Path | None = None, hook=None) -> dict:
    """yt-dlp の設定。work を渡すと、そのフォルダへダウンロードする設定を足す。"""
    opts = {
        "format": AUDIO_FORMAT,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": logger,
        "socket_timeout": 30,
        "retries": 3,
        "cachedir": False,
        "ffmpeg_location": audio.find_ffmpeg(),
    }
    if work is not None:
        opts.update({
            "outtmpl": str(work / "source.%(ext)s"),
            "paths": {"home": str(work), "temp": str(work)},
            "max_filesize": settings.max_filesize,
            "progress_hooks": [hook],
            "overwrites": True,
            "concurrent_fragment_downloads": FRAGMENT_THREADS,
        })
    return opts


def _check_info(info: dict | None) -> dict:
    """ダウンロードを始める前に、対応していないもの(プレイリスト・ライブ・長すぎる)を断る。"""
    if info is None:
        raise UserError("情報を取得できませんでした。")
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise UserError("プレイリストには対応していません。1本の動画・曲の URL を入力してください。")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise UserError("ライブ配信には対応していません。")
    _check_duration(info.get("duration"))
    return info


_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


class _Extractor:
    """YouTube の動画情報を調べるための yt-dlp を、アプリが動いている間ずっと使い回す。

    使い回すと 2 曲目からは、YouTube のプレーヤーのプログラム(約 0.8MB)の取り直しと、
    接続のやり直しが要らなくなる(通信が 5 回から 4 回に減り、0.5〜1 秒ほど速い。
    ただし YouTube の応答時間そのものが数秒単位で揺れるので、毎回はっきり分かるほどの差ではない)。
    ダウンロードそのものは、曲ごとに保存先などが違うので毎回新しい yt-dlp で行う。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ydl = None
        self._logger: _YdlLogger | None = None
        self._used = False

    def _open(self):
        import yt_dlp  # 起動を速くするため遅延 import
        if self._ydl is None:
            self._logger = _YdlLogger()
            self._ydl = yt_dlp.YoutubeDL(_ydl_opts(self._logger))
            self._used = False
        return self._ydl

    def _close(self) -> None:
        ydl, self._ydl, self._logger = self._ydl, None, None
        if ydl is not None:
            try:
                ydl.close()
            except Exception:  # noqa: BLE001
                pass

    def warm(self) -> None:
        """最初の読み込みを待たせないよう、前もって yt-dlp を用意しておく(通信はしない)。"""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._open()
        except Exception:  # noqa: BLE001  (用意できなくても、読み込むときにやり直す)
            self._close()
        finally:
            self._lock.release()

    def extract(self, url: str, logger: _YdlLogger) -> tuple[dict | None, bool] | None:
        """動画の情報を調べて (情報, 使い回しだったか) を返す。ほかの読み込みが使っている最中なら None。

        失敗したら例外(DownloadError など)。そのときは覚えている内容を捨てる。
        例外の reused 属性が True なら、使い回していたものでの失敗(新しく作り直せば直るかもしれない)。
        """
        if not self._lock.acquire(blocking=False):
            return None
        try:
            ydl = self._open()
            reused, self._used = self._used, True
            self._logger.warnings = logger.warnings
            try:
                return ydl.extract_info(url, download=False), reused
            except BaseException as e:
                self._close()
                try:
                    e.reused = reused
                except AttributeError:
                    pass
                raise
        finally:
            if self._logger is not None:
                self._logger.warnings = []
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._close()


_extractor = _Extractor()


def _fetch(url: str, work: Path, hook, logger: _YdlLogger) -> dict:
    """yt-dlp で動画の情報を調べ、音声を work にダウンロードして、情報を返す。"""
    import yt_dlp  # 起動を速くするため遅延 import

    def fresh() -> dict:
        # 1 つの yt-dlp で、調べるところからダウンロードまで行う(これまでどおりのやり方)
        with yt_dlp.YoutubeDL(_ydl_opts(logger, work, hook)) as ydl:
            info = _check_info(ydl.extract_info(url, download=False))
            return ydl.process_ie_result(info, download=True)

    if not _is_youtube(url):
        return fresh()
    try:
        got = _extractor.extract(url, logger)
    except yt_dlp.utils.DownloadError as e:
        if not getattr(e, "reused", False):
            raise  # 作ったばかりの yt-dlp でも駄目だった(動画が無い、など)
        got = None
    except Exception:  # noqa: BLE001  (使い回しがうまくいかないときは、これまでどおりのやり方で)
        got = None
    if got is None:
        del logger.warnings[:]
        return fresh()
    info = _check_info(got[0])
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(logger, work, hook)) as ydl:
            return ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError:
        # 念のため、これまでどおりのやり方でもう一度試す
        del logger.warnings[:]
        for p in list(work.iterdir()):
            remove_path(p)
        return fresh()


def download_url(url: str, progress=None) -> dict:
    """URL から音声を取得してキャッシュに保存し、メタ情報を返す。"""
    import yt_dlp  # 起動を速くするため遅延 import

    url = validate_url(url)
    if not audio.find_ffmpeg():
        raise UserError(MSG_FFMPEG, 500)
    source_id = hashlib.sha256(("url:" + url).encode()).hexdigest()[:20]
    if source_path(source_id) and (meta := read_meta(source_id)):
        _touch(sources_dir() / source_id / "meta.json")
        return meta

    work = sources_dir() / f".tmp-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    too_big = f"ファイルが大きすぎます(上限 {settings.max_filesize // 1024 // 1024}MB)。"

    def hook(d):
        # Content-Length の無い配信(ネットラジオ等)もここで止める
        if d.get("status") != "downloading":
            return
        done = d.get("downloaded_bytes") or 0
        if done > settings.max_filesize:
            raise UserError(too_big)
        if time.monotonic() - started > settings.max_download_seconds:
            raise UserError("ダウンロードに時間がかかりすぎたため中止しました。"
                            "ライブ配信やネットラジオなど、終わりの無い音声には対応していません。")
        if progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(done / total, 1.0))

    logger = _YdlLogger()
    try:
        try:
            info = _fetch(url, work, hook, logger)
        except yt_dlp.utils.DownloadError as e:
            raise UserError(_translate_ytdlp_error(str(e), logger.warnings), 502) from None

        files = [p for p in work.iterdir()
                 if p.name.startswith("source.") and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise UserError(
                f"ダウンロードできませんでした(ファイルが大きすぎる可能性があります。上限 "
                f"{settings.max_filesize // 1024 // 1024}MB)。", 502)
        src = files[0]
        if src.stat().st_size > settings.max_filesize:
            raise UserError(f"ファイルが大きすぎます(上限 {settings.max_filesize // 1024 // 1024}MB)。")
        pr = audio.probe(src)
        if not pr["has_audio"]:
            raise UserError("音声が含まれていないようです。別の URL をお試しください。")
        duration = pr["duration"] or info.get("duration")
        _check_duration(duration)
        title = (info.get("title") or "").strip() or "audio"
        final_src = work / ("source" + src.suffix.lower())
        meta = {"id": source_id, "title": title, "duration": duration,
                "source": "url", "url": url, "created": time.time(),
                "direct": direct_media_type(final_src, pr)}
        final = sources_dir() / source_id
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        src.rename(final_src)
        for p in list(work.iterdir()):
            if not p.name.startswith("source."):
                p.unlink(missing_ok=True)
        (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(work, final)
        return meta
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


ALLOWED_UPLOAD_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
                      ".webm", ".mp4", ".mov", ".mkv", ".wma", ".aif", ".aiff", ".caf", ".3gp"}


def save_upload(upload: UploadFile) -> dict:
    if not audio.find_ffmpeg():
        raise UserError(MSG_FFMPEG, 500)
    name = Path(upload.filename or "audio").name
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXT:
        raise UserError("対応していないファイル形式です。mp3 / m4a / wav / flac などの音声ファイルを選んでください。")
    work = sources_dir() / f".tmp-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        dst = work / ("source" + ext)
        h = hashlib.sha256(b"file:")
        size = 0
        with open(dst, "wb") as f:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_filesize:
                    raise UserError(f"ファイルが大きすぎます(上限 {settings.max_filesize // 1024 // 1024}MB)。")
                h.update(chunk)
                f.write(chunk)
        if size == 0:
            raise UserError("ファイルが空です。")
        source_id = h.hexdigest()[:20]
        if source_path(source_id) and (meta := read_meta(source_id)):
            return meta
        pr = audio.probe(dst)
        if not pr["has_audio"]:
            raise UserError("音声ファイルとして読み込めませんでした。別のファイルをお試しください。")
        _check_duration(pr["duration"])
        title = Path(name).stem.strip() or "audio"
        meta = {"id": source_id, "title": title, "duration": pr["duration"],
                "source": "file", "created": time.time(), "direct": direct_media_type(dst, pr)}
        (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        final = sources_dir() / source_id
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(work, final)
        return meta
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 変換
@dataclass(eq=False)
class _Render:
    """実行中(または順番待ち)の変換 1 件。"""
    source_id: str
    semitones: int
    background: bool  # True = 先回り変換
    cancel: threading.Event = field(default_factory=threading.Event)
    prepare: bool = False  # True = 保存用に前もって用意している変換(リアルタイム再生のとき。誰も待っていない)


_render_locks: dict[str, threading.Lock] = {}
_render_guard = threading.Condition()  # _render_locks / _renders / _encodes / _foreground を守る
_renders: list[_Render] = []
_encodes: set[threading.Event] = set()  # 実行中の MP3 作成を取り消すためのイベント
_foreground = 0  # 利用者が待っている変換・MP3 作成の数。1 つでもあれば先回り変換は動かさない
_render_sem = threading.Semaphore(2)  # 同時変換数の上限


def ensure_render(source_id: str, semitones: int, *, background: bool = False,
                  supersede: bool = False, prepare: bool = False,
                  wanted: Callable[[], bool] | None = None) -> Path:
    """キーを変えた音声(再生用の WAV)を(無ければ変換して)用意し、そのパスを返す。

    semitones が 0 のときは、音源を WAV に直しただけのもの(音源をそのまま再生できないとき用)。

    background=True は先回り変換。利用者が待っている変換があるときは始めず、
    途中で利用者の変換が来たら取り消される(どちらも audio.RenderCancelled)。
    wanted は「この先回り変換(または prepare の変換)がまだ必要か」を返す関数で、不要になっていたら始めない。
    supersede=True は「この音源の、ほかのキーへの変換はもう要らない」という意味で、
    実行中のものを取り消す(スライダーを動かして別のキーに変えたとき)。
    prepare=True は、画面がリアルタイムでキーを変えて再生しているときに、選ばれたキーを
    保存用に前もって変換しておくもの(`Preparer`)。聴くのには使わないので、利用者が待っている
    変換としては数えず(先回り変換も止めない)、ほかの変換も取り消さない(画面が再生のために
    待っている原曲キーの WAV を止めてはいけないため。前に選ばれたキーの分は `Preparer` がやめる)。
    先回り変換ではないので、「ダウンロード」を押したときの MP3 作りに取り消されることはなく、
    押した側は同じキーのこの変換が済むのを待って、それを使う。
    """
    check_id(source_id)
    try:
        audio.validate_semitones(semitones)
    except ValueError:
        raise UserError("キーは -12 から +12 の整数で指定してください。") from None
    src = source_path(source_id)
    if not src:
        raise UserError("音声が見つかりません(キャッシュが削除された可能性があります)。もう一度読み込んでください。", 404)
    out = render_path(source_id, semitones)
    if out.exists():
        _touch(out)
        return out
    me = _Render(source_id, semitones, background, prepare=prepare)
    global _foreground
    with _render_guard:
        if background:
            if _foreground or (wanted is not None and not wanted()):
                raise audio.RenderCancelled()
        elif prepare:
            if wanted is not None and not wanted():
                raise audio.RenderCancelled()
        else:
            _foreground += 1
            for r in _renders:
                same = r.source_id == source_id and r.semitones == semitones
                if not same and (r.background or (supersede and r.source_id == source_id)):
                    r.cancel.set()
        _renders.append(me)
        lock = _render_locks.setdefault(out.name, threading.Lock())
    try:
        with lock:
            if out.exists():
                _touch(out)
                return out
            if not audio.find_ffmpeg():
                raise UserError(MSG_FFMPEG, 500)
            if background:
                _render_to(src, out, me)
            else:
                with _render_sem:
                    _render_to(src, out, me)
            _touch(src.parent / "meta.json")
        tidy_cache(out, src.parent)  # 変換結果は大きいので、増えたらその場で古いものを片付ける
        return out
    finally:
        with _render_guard:
            _renders.remove(me)
            if not background and not prepare:
                _foreground -= 1
            _render_guard.notify_all()


def _render_to(src: Path, out: Path, me: _Render) -> None:
    if me.cancel.is_set():
        raise audio.RenderCancelled()
    try:
        audio.render(src, out, me.semitones, engine=settings.engine, jobs=settings.jobs,
                     cancel=me.cancel)
    except audio.AudioError:
        raise UserError("音声の変換に失敗しました。別の音源でお試しください。", 500) from None


def ensure_mp3(source_id: str, semitones: int, *, cancel: threading.Event | None = None,
               background: bool = False) -> Path:
    """保存(ダウンロード)用の MP3 を(無ければ作って)用意し、そのパスを返す。

    原曲キーは音源から直接、それ以外は変換済みの音声(WAV)から作る。
    ふつうは「ダウンロード」を押した人が待っているので、作っている間は先回り変換に譲ってもらう
    (利用者が待っている変換と同じ扱い)。
    background=True は、聴いている間に前もって作っておくとき用。急ぎではないので先回り変換を
    止めず、変換済みの音声が無ければ(そのために変換を始めたりせず)audio.RenderCancelled を送出する。
    cancel がセットされると途中でやめて audio.RenderCancelled を送出する。
    """
    check_id(source_id)
    try:
        audio.validate_semitones(semitones)
    except ValueError:
        raise UserError("キーは -12 から +12 の整数で指定してください。") from None
    src = source_path(source_id)
    if not src:
        raise UserError("音声が見つかりません(キャッシュが削除された可能性があります)。もう一度読み込んでください。", 404)
    out = mp3_path(source_id, semitones)
    if out.exists():
        _touch(out)
        return out
    cancel = cancel or threading.Event()
    global _foreground
    with _render_guard:
        lock = _render_locks.setdefault(out.name, threading.Lock())
        if not background:
            _foreground += 1
            for r in _renders:
                if r.background:
                    r.cancel.set()
    try:
        with lock:
            if out.exists():
                _touch(out)
                return out
            if not audio.find_ffmpeg():
                raise UserError(MSG_FFMPEG, 500)
            if semitones == 0:
                wav = None
            elif not background:
                wav = ensure_render(source_id, semitones)
            elif (wav := render_path(source_id, semitones)).exists():
                _touch(wav)
            else:
                raise audio.RenderCancelled()
            with _render_guard:
                _encodes.add(cancel)
            try:
                if wav is None:
                    audio.encode_mp3(src, out, cancel=cancel)
                else:
                    audio.encode_mp3(wav, out, tags_from=src, cancel=cancel)
            except audio.AudioError:
                raise UserError("MP3 の作成に失敗しました。もう一度お試しください。", 500) from None
            finally:
                with _render_guard:
                    _encodes.discard(cancel)
                    _render_guard.notify_all()
            _touch(src.parent / "meta.json")
    finally:
        if not background:
            with _render_guard:
                _foreground -= 1
                _render_guard.notify_all()
    tidy_cache(out, src.parent)
    return out


def playback(source_id: str, semitones: int, compat: bool = False, *,
             supersede: bool = False) -> tuple[Path, str, bool]:
    """再生する音声を(無ければ変換して)用意し、(ファイル, 種類(Content-Type), 音源そのものか) を返す。

    原曲キーで、音源がそのままブラウザで再生できるものなら、音源そのものを返す(変換しない)。
    compat=True は「そのままでは再生できなかった」という画面からの知らせで、WAV に直したものを返す。
    supersede は ensure_render と同じ。
    """
    if semitones == 0 and not compat:
        media_type = direct_type(source_id)
        src = source_path(source_id) if media_type else None
        if src is not None:
            _touch(src.parent / "meta.json")
            return src, media_type, True
    return ensure_render(source_id, semitones, supersede=supersede), "audio/wav", False


def is_ready(source_id: str, semitones: int, compat: bool = False) -> bool:
    """このキーの音声を、変換を待たずにすぐ再生できるか。"""
    if semitones == 0 and not compat and direct_type(source_id):
        return True
    return render_path(source_id, semitones).exists()


def cancel_all_renders() -> None:
    with _render_guard:
        for r in _renders:
            r.cancel.set()
        for e in _encodes:
            e.set()


class Prefetcher:
    """聴いているキーの近くのキーを、空いている時間に先回りして変換しておく。

    +/− を 1 つ・2 つ動かしたときに待たずに済むようにするためのもの。
    利用者が待っている変換が始まったら譲り(取り消され)、終わってから続きをやる。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._targets: list[tuple[str, int]] = []
        self._thread: threading.Thread | None = None
        self._busy = False
        self._stopping = False

    def schedule(self, source_id: str, keys: list[int]) -> None:
        """先回りする対象を入れ替える(前に頼まれて、まだやっていない分は捨てる)。"""
        targets = [(source_id, k) for k in keys]
        with self._cv:
            self._targets = targets
            self._stopping = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="prefetch", daemon=True)
                self._thread.start()
            self._cv.notify_all()
        with _render_guard:
            for r in _renders:
                if r.background and (r.source_id, r.semitones) not in targets:
                    r.cancel.set()

    def drop(self, source_id: str) -> None:
        """この音源の先回りをやめる(まだやっていない分は捨て、実行中のものは取り消す)。"""
        with self._cv:
            self._targets = [t for t in self._targets if t[0] != source_id]
            self._cv.notify_all()
        with _render_guard:
            for r in _renders:
                if r.background and r.source_id == source_id:
                    r.cancel.set()

    def stop(self) -> None:
        with self._cv:
            self._targets = []
            self._stopping = True
            self._cv.notify_all()
        with _render_guard:
            for r in _renders:
                if r.background:
                    r.cancel.set()
            _render_guard.notify_all()

    def wait_idle(self, timeout: float = 60) -> bool:
        """先回り変換がすべて終わるまで待つ(テスト用)。"""
        with self._cv:
            return self._cv.wait_for(lambda: not self._targets and not self._busy, timeout)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._targets and not self._stopping:
                    self._busy = False
                    self._cv.notify_all()
                    self._cv.wait()
                if self._stopping:
                    self._busy = False
                    self._cv.notify_all()
                    return
                target = self._targets[0]
                self._busy = True
            with _render_guard:  # 利用者の変換が終わるまで待つ
                while _foreground and not self._stopping:
                    _render_guard.wait(1.0)
            keep = False
            try:
                # 対象が入れ替わっていたら始めない(schedule と行き違いにならないよう、
                # 変換の登録と同時に確かめる)
                ensure_render(*target, background=True,
                              wanted=lambda: not self._stopping and target in self._targets)
            except audio.RenderCancelled:
                keep = True  # 譲っただけ。対象に残っていれば後でやり直す
            except Exception:  # noqa: BLE001  (先回りの失敗は利用者には見せない)
                pass
            if not keep:
                with self._cv:
                    if target in self._targets:
                        self._targets.remove(target)


prefetcher = Prefetcher()
_last_key: dict[str, int] = {}  # 音源ごとの、直前に聴いていたキー(動かしている向きを知るため)


def schedule_prefetch(source_id: str, semitones: int, compat: bool = False) -> None:
    """いま聴いているキーの近く(近い順に、動かしている向きを先に)を先回り変換の対象にする。"""
    span = int(settings.prefetch)
    if span <= 0:
        return
    prev = _last_key.get(source_id)
    if len(_last_key) > 64:
        _last_key.clear()
    _last_key[source_id] = semitones
    duration = (read_meta(source_id) or {}).get("duration")
    if isinstance(duration, (int, float)) and duration > settings.prefetch_long:
        span = 1
    step = -1 if prev is not None and semitones < prev else 1
    near = [semitones + d * sign for d in range(1, span + 1) for sign in (step, -step)]
    keys = [k for k in near
            if audio.MIN_SEMITONES <= k <= audio.MAX_SEMITONES and not is_ready(source_id, k, compat)]
    prefetcher.schedule(source_id, keys)


class Mp3Maker:
    """聴いているキーの保存用 MP3 を、少し待ってから前もって作っておく。

    MP3 は「ダウンロード」を押したときに作れば足りるが、長い曲だと数秒かかるので、
    同じキーをしばらく聴いていたら作っておき、押したときに待たせないようにする。
    別のキーに変えられたら、作りかけのものはやめる(最後に選ばれたキーの分だけ作る)。
    """

    delay = 2.0  # 同じキーをこの秒数聴いていたら作り始める

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._target: tuple[str, int] | None = None   # 次に作るもの
        self._due = 0.0                                # 作り始める時刻
        self._running: tuple[str, int] | None = None  # いま作っているもの
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    def schedule(self, source_id: str, semitones: int) -> None:
        target = (source_id, semitones)
        with self._cv:
            self._stopping = False
            if self._running == target:
                self._target = None
            else:
                if self._cancel is not None:
                    self._cancel.set()
                self._target, self._due = target, time.monotonic() + self.delay
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="mp3", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def keep_only(self, source_id: str, semitones: int) -> None:
        """このキー以外の分は(待っているものも、作りかけも)やめる。

        別のキーが選ばれたらすぐ呼ぶ。そのキーの変換が済むまで待つと、もう聴いていないキーの
        MP3 作りが、利用者の待っている変換と CPU を取り合ってしまう。
        """
        target = (source_id, semitones)
        with self._cv:
            if self._target != target:
                self._target = None
            if self._running != target and self._cancel is not None:
                self._cancel.set()
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._target = None
            self._stopping = True
            if self._cancel is not None:
                self._cancel.set()
            self._cv.notify_all()

    def wait_idle(self, timeout: float = 60) -> bool:
        """前もって作る分がすべて終わるまで待つ(テスト用)。"""
        with self._cv:
            return self._cv.wait_for(lambda: self._target is None and self._running is None, timeout)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._stopping:
                    if self._target is None:
                        self._cv.wait()
                    elif (left := self._due - time.monotonic()) > 0:
                        self._cv.wait(left)
                    else:
                        break
                if self._stopping:
                    return
                target, self._target = self._target, None
                self._running, self._cancel = target, threading.Event()
                cancel = self._cancel
            try:
                ensure_mp3(*target, cancel=cancel, background=True)
            except Exception:  # noqa: BLE001  (作れなくても、ダウンロードのときに作り直すだけ)
                pass
            finally:
                with self._cv:
                    self._running = self._cancel = None
                    self._cv.notify_all()


mp3_maker = Mp3Maker()


def schedule_mp3(source_id: str, semitones: int) -> None:
    if int(settings.prefetch) > 0 and not mp3_path(source_id, semitones).exists():
        mp3_maker.schedule(source_id, semitones)


class Preparer:
    """画面がリアルタイムでキーを変えて再生しているとき(`/api/render` の live)に、
    選ばれたキーを保存用に裏で変換しておく。

    聴くのには使わない(画面は原曲キーを再生し、ブラウザの中で音の高さを変えている)ので、
    返事は変換を待たずに返す。ここで作った WAV は「ダウンロード」を押したときの MP3 の材料になる。
    - 最後に選ばれたキーの分だけ作る。別のキーが選ばれたら、作りかけはその場でやめる。
    - 利用者が待っている変換とは数えない(`ensure_render` の prepare)。「ダウンロード」を押すと、
      押した側(利用者が待っている MP3 作り)が、同じキーのこの変換が済むのを待って使う。
    - 済んだら、まだそのキーが選ばれていれば、保存用の MP3 を前もって作る予定に入れる(`schedule_mp3`)。
    原曲キーの MP3 は音源から直接作るので、変換は要らない(作りかけをやめ、MP3 の予定だけ入れる)。
    MP3 の予定は、選ばれたキーの入れ替えと同じロックの中で入れる(行き違いで古いキーの MP3 を
    作り始めないように)。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._wanted: tuple[str, int] | None = None   # 最後に選ばれたキー
        self._target: tuple[str, int] | None = None   # 次に作るもの
        self._running: tuple[str, int] | None = None  # いま作っているもの
        self._thread: threading.Thread | None = None
        self._stopping = False

    def schedule(self, source_id: str, semitones: int) -> None:
        target = (source_id, semitones)
        with self._cv:
            self._stopping = False
            self._wanted = target
            mp3_maker.keep_only(source_id, semitones)
            if semitones == 0:
                self._target = None
                schedule_mp3(source_id, 0)
            else:
                self._target = None if self._running == target else target
            if self._target is not None and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="prepare", daemon=True)
                self._thread.start()
            self._cv.notify_all()
        with _render_guard:  # 前に選ばれていたキーの作りかけはやめる
            for r in _renders:
                if r.prepare and (r.source_id, r.semitones) != target:
                    r.cancel.set()

    def stop(self) -> None:
        with self._cv:
            self._target = self._wanted = None
            self._stopping = True
            self._cv.notify_all()
        with _render_guard:
            for r in _renders:
                if r.prepare:
                    r.cancel.set()

    def wait_idle(self, timeout: float = 60) -> bool:
        """作る分がすべて終わるまで待つ(テスト用)。"""
        with self._cv:
            return self._cv.wait_for(lambda: self._target is None and self._running is None, timeout)

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._target is None and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    return
                target, self._target = self._target, None
                self._running = target
            try:
                ensure_render(*target, prepare=True, wanted=lambda: self._wanted == target)
                with self._cv:
                    if self._wanted == target:
                        schedule_mp3(*target)
            except Exception:  # noqa: BLE001  (作れなくても、ダウンロードのときに作り直すだけ)
                pass
            finally:
                with self._cv:
                    self._running = None
                    self._cv.notify_all()


preparer = Preparer()


def safe_filename(title: str, semitones: int, ext: str = ".mp3") -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title or "audio").strip(" .")
    t = re.sub(r"\s+", " ", t)[:80].strip(" .") or "audio"
    key = "±0" if semitones == 0 else f"{semitones:+d}"
    return f"{t} ({key}){ext}"


# ---------------------------------------------------------------- ジョブ
@dataclass
class Job:
    id: str
    status: str = "running"  # running / done / error
    progress: float | None = None
    phase: str = "ダウンロード中"
    result: dict | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event, repr=False)  # 終わったらセットされる


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _run_job(job: Job, url: str) -> None:
    def progress(p: float) -> None:
        # 並行してダウンロードしていると数字が前後することがあるので、戻らないようにする
        job.progress = max(job.progress or 0.0, p)
    try:
        meta = download_url(url, progress=progress)
        job.result = meta
        job.status = "done"
        job.progress = 1.0
    except UserError as e:
        job.error = e.message
        job.status = "error"
    except Exception:  # noqa: BLE001
        job.error = "予期しないエラーが発生しました。もう一度お試しください。"
        job.status = "error"
    job.done.set()  # 待っている画面に、すぐ知らせる
    if job.result:
        tidy_cache(sources_dir() / job.result["id"])
    else:
        tidy_cache()


def start_job(url: str) -> Job:
    validate_url(url)
    if not audio.find_ffmpeg():
        raise UserError(MSG_FFMPEG, 500)
    job = Job(id=uuid.uuid4().hex)
    with _jobs_lock:
        cutoff = time.time() - 3600
        for k in [k for k, j in _jobs.items() if j.created < cutoff]:
            _jobs.pop(k, None)
        _jobs[job.id] = job
    threading.Thread(target=_run_job, args=(job, url), daemon=True).start()
    return job


# ---------------------------------------------------------------- API
@asynccontextmanager
async def _lifespan(_app):
    try:
        cleanup_cache(startup=True)
    except OSError:
        pass
    # 最初の読み込みを少しでも速くするため、yt-dlp を前もって用意しておく(通信はしない)
    threading.Thread(target=_extractor.warm, name="warm", daemon=True).start()
    yield
    # 終了時: 動いている変換(ffmpeg)を止め、作業ファイルを片付け終わるのを少し待つ
    prefetcher.stop()
    mp3_maker.stop()
    preparer.stop()
    cancel_all_renders()
    with _render_guard:
        _render_guard.wait_for(lambda: not _renders and not _encodes, timeout=5)


app = FastAPI(title="キー変更アプリ", docs_url=None, redoc_url=None, lifespan=_lifespan)

# DNS リバインディング対策: Host ヘッダーが自分(127.0.0.1 / localhost)以外のリクエストは拒否する。
# 別の名前・アドレスで公開したいときは KEYCHANGER_ALLOWED_HOSTS(カンマ区切り)で追加できる。
ALLOWED_HOSTS: set[str] = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("KEYCHANGER_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _host_of(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):  # [::1]:8000
        return header[1:header.find("]")] if "]" in header else header
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


@app.middleware("http")
async def _check_host(request, call_next):
    host = _host_of(request.headers.get("host", ""))
    if "*" not in ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": f"このアドレスからは利用できません。{_app_url} を開いてください。"},
                            status_code=400)
    return await call_next(request)


@app.exception_handler(UserError)
async def _user_error(_, exc: UserError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(audio.RenderCancelled)
async def _render_cancelled(_, exc: audio.RenderCancelled):
    # 別のキーに変えられて不要になった変換。画面側は新しい方の結果だけを使う
    return JSONResponse({"detail": "変換を取り消しました。"}, status_code=409)


class LoadRequest(BaseModel):
    url: str


class RenderRequest(BaseModel):
    id: str
    semitones: int
    compat: bool = False  # 音源をそのままでは再生できなかったので、WAV に直したものを使う
    peek: bool = False    # すぐ再生できるときだけ応える(変換は始めない)
    # 画面がリアルタイムでキーを変えて再生している(原曲キーだけを再生する)。このキーは保存用に
    # 裏で用意するだけで、返事は変換を待たない。近くのキーの先回り変換もしない
    live: bool = False


class Mp3Request(BaseModel):
    id: str
    semitones: int


@app.get("/api/status")
def api_status():
    ff = audio.find_ffmpeg()
    try:
        from yt_dlp.version import __version__ as ytv
    except Exception:  # noqa: BLE001
        ytv = None
    engine = settings.engine or (audio.choose_engine(ff) if ff else None)
    return {"ffmpeg": bool(ff), "engine": engine, "yt_dlp": ytv,
            "deno": bool(shutil.which("deno")),
            "max_minutes": int(settings.max_duration // 60),
            "max_mb": settings.max_filesize // 1024 // 1024}


@app.post("/api/load")
def api_load(req: LoadRequest):
    job = start_job(req.url)
    return {"job": job.id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, wait: float = Query(0, ge=0, le=30)):
    """読み込みの進み具合。wait(秒)を付けると、終わるまで最長その秒数だけ待ってから応える。"""
    if not JOB_RE.fullmatch(job_id):
        raise UserError("ジョブが見つかりません。", 404)
    job = _jobs.get(job_id)
    if not job:
        raise UserError("ジョブが見つかりません。", 404)
    if wait:
        job.done.wait(wait)
    return {"status": job.status, "progress": job.progress, "result": job.result, "error": job.error}


@app.post("/api/upload")
def api_upload(file: UploadFile = File(...)):
    meta = save_upload(file)
    tidy_cache(sources_dir() / meta["id"])
    return meta


def _audio_url(source_id: str, semitones: int, download: bool = False, compat: bool = False) -> str:
    u = f"/api/audio/{source_id}?semitones={semitones}"
    if download:
        return u + "&download=1"
    return u + "&compat=1" if compat else u


def _check_key(semitones: int) -> None:
    try:
        audio.validate_semitones(semitones)
    except ValueError:
        raise UserError("キーは -12 から +12 の整数で指定してください。") from None


@app.post("/api/render")
def api_render(req: RenderRequest):
    """このキーの音声を再生できるようにして、その URL を返す。"""
    check_id(req.id)
    _check_key(req.semitones)
    if req.live:
        return _prepare_live(req.id, req.semitones)
    if req.peek and not is_ready(req.id, req.semitones, req.compat):
        if not source_path(req.id):
            raise UserError("音声が見つかりません(キャッシュが削除された可能性があります)。もう一度読み込んでください。", 404)
        return {"ready": False}
    mp3_maker.keep_only(req.id, req.semitones)
    _, _, direct = playback(req.id, req.semitones, req.compat, supersede=True)
    schedule_prefetch(req.id, req.semitones, req.compat)
    schedule_mp3(req.id, req.semitones)
    meta = read_meta(req.id) or {}
    return {"ready": True, "direct": direct,
            "url": _audio_url(req.id, req.semitones, compat=req.compat and req.semitones == 0),
            "download_url": _audio_url(req.id, req.semitones, download=True),
            "filename": safe_filename(meta.get("title", "audio"), req.semitones),
            "mp3": mp3_path(req.id, req.semitones).exists()}


def _prepare_live(source_id: str, semitones: int) -> dict:
    """リアルタイム再生の画面から: 選ばれたキーを保存用に用意し始め、ダウンロード用の情報をすぐ返す。

    聴いているのは原曲キー(`/api/audio/<ID>?semitones=0`)なので、近くのキーの先回り変換は
    要らない(この音源の分はやめる)。この返事が再生に使われることはない。
    """
    if not source_path(source_id):
        raise UserError("音声が見つかりません(キャッシュが削除された可能性があります)。もう一度読み込んでください。", 404)
    prefetcher.drop(source_id)
    preparer.schedule(source_id, semitones)
    meta = read_meta(source_id) or {}
    return {"live": True,
            "download_url": _audio_url(source_id, semitones, download=True),
            "filename": safe_filename(meta.get("title", "audio"), semitones),
            "mp3": mp3_path(source_id, semitones).exists()}


@app.post("/api/mp3")
def api_mp3(req: Mp3Request):
    """保存用の MP3 を(無ければ)作る。画面は、これが済んでからダウンロードを始める。"""
    check_id(req.id)
    ensure_mp3(req.id, req.semitones)
    return {"ready": True}


@app.get("/api/audio/{source_id}")
def api_audio(source_id: str, semitones: int = Query(0, ge=-12, le=12), download: int = 0,
              compat: int = 0):
    check_id(source_id)
    if download:
        path, media_type = ensure_mp3(source_id, semitones), "audio/mpeg"
    else:
        path, media_type, _ = playback(source_id, semitones, bool(compat))
    meta = read_meta(source_id) or {}
    name = safe_filename(meta.get("title", "audio"), semitones, path.suffix)
    disp = "attachment" if download else "inline"
    headers = {"Content-Disposition": f"{disp}; filename=\"{_ascii_name(name)}\"; filename*=UTF-8''{quote(name)}",
               "Cache-Control": "no-cache"}
    return FileResponse(path, media_type=media_type, headers=headers)


def _ascii_name(name: str) -> str:
    s = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return s


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ---------------------------------------------------------------- 起動
DEFAULT_PORT = 8000
PORT_TRIES = 20      # 既定のポートが使用中のとき、続きの番号をいくつまで試すか(8000 なら 8019 まで)
BROWSER_DELAY = 1.5  # 起動してからブラウザを開くまでの秒数
_app_url = f"http://127.0.0.1:{DEFAULT_PORT}"  # 案内に出すアドレス。main() が実際の番号に直す

# 「このポートは使えない」を表すエラー番号。Windows は予約済み・占有中のポートをアクセス拒否で断ることがある
_PORT_BUSY = (errno.EADDRINUSE, errno.EACCES)


class PortInUse(Exception):
    def __init__(self, port: int):
        super().__init__(f"port {port} is in use")
        self.port = port


def _bind(host: str, port: int) -> socket.socket:
    """host:port で待ち受けを始めたソケットを返す。使えなければ OSError。"""
    family, kind, proto, _, addr = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0]
    sock = socket.socket(family, kind, proto)
    try:
        if os.name != "nt":
            # 終了直後でもすぐ起動し直せるようにする。Windows でこれを付けると、
            # 使用中のポートまで開けてしまう(別のアプリと混線する)ので付けない
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(addr)
        sock.listen()
    except BaseException:
        sock.close()
        raise
    return sock


def open_server_socket(host: str, port: int, *, fallback: bool) -> socket.socket:
    """待ち受け用のソケットを開く。

    port が使用中のとき、fallback=True なら続きの番号を順に試し、それも全部だめなら OS に
    空き番号を選ばせる。fallback=False なら(利用者が番号を指定したとき)PortInUse にする。
    空きを調べてから開くのではなく、開けたソケットをそのままサーバーに渡すので、
    調べたあとで他のアプリに取られることがない。
    """
    candidates = [port]
    if fallback and port:
        candidates += list(range(port + 1, min(port + PORT_TRIES, 65536))) + [0]
    for candidate in candidates:
        try:
            return _bind(host, candidate)
        except OSError as e:
            if e.errno not in _PORT_BUSY:
                raise
    raise PortInUse(port)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    env_port = os.environ.get("KEYCHANGER_PORT", "").strip()
    if env_port and not env_port.isdigit():
        print(f"[エラー] 環境変数 KEYCHANGER_PORT は数字で指定してください(今の値: {env_port})。")
        raise SystemExit(1)
    ap = argparse.ArgumentParser(description="キー変更アプリ")
    ap.add_argument("--host", default=os.environ.get("KEYCHANGER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else None,
                    help=f"待ち受けるポート番号。省略すると {DEFAULT_PORT}(使用中なら空いている番号)")
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = ap.parse_args(argv)

    # 番号を指定されたときはその番号だけを使う。指定がなければ、使用中のとき空いている番号へ移る
    chosen = args.port is not None
    wanted = args.port if chosen else DEFAULT_PORT
    if not 0 <= wanted <= 65535:
        print(f"[エラー] ポート番号は 0 から 65535 の間で指定してください(今の値: {wanted})。")
        raise SystemExit(1)
    try:
        sock = open_server_socket(args.host, wanted, fallback=not chosen)
    except PortInUse:
        print(f"[エラー] ポート {wanted} はほかのアプリが使用中のため、起動できません。")
        if chosen:
            print("        ポート番号の指定(KEYCHANGER_PORT または --port)をやめると、"
                  "空いている番号を自動で選びます。")
        raise SystemExit(1) from None
    except OSError as e:
        print(f"[エラー] {args.host} のポート {wanted} で待ち受けを始められません: {e}")
        raise SystemExit(1) from None
    port = sock.getsockname()[1]

    if args.host not in ("0.0.0.0", "::"):
        ALLOWED_HOSTS.add(args.host.strip("[]").lower())
    else:
        print("[注意] すべてのネットワークに公開しています。KEYCHANGER_ALLOWED_HOSTS に"
              "この PC のアドレスを指定しないと、他の端末からは開けません。")
    global _app_url
    url = _app_url = f"http://127.0.0.1:{port}"
    print("=" * 50)
    print(" キー変更アプリを起動しました")
    if wanted and port != wanted:
        print(f" (ポート {wanted} はほかのアプリが使用中のため、{port} 番を使います)")
    print(f" ブラウザで {url} を開いてください")
    print(" 終了するにはこの画面で Ctrl + C を押すか、ウィンドウを閉じます")
    print("=" * 50)
    if not audio.find_ffmpeg():
        print("[注意] ffmpeg が見つかりません。README の手順でインストールしてください。")
    if args.open:
        timer = threading.Timer(BROWSER_DELAY, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    if not server.started:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
