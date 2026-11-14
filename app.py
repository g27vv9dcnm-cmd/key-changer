"""キー変更アプリ(ローカル専用 Web アプリ)

起動:  python app.py --open
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
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
    # 聴いているキーの隣(±1)を先回りして変換しておく
    prefetch: bool = os.environ.get("KEYCHANGER_PREFETCH", "1") != "0"


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
    p = sources_dir() / source_id / "meta.json"
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def render_path(source_id: str, semitones: int) -> Path:
    check_id(source_id)
    audio.validate_semitones(semitones)
    return renders_dir() / f"{source_id}_{semitones:+d}.mp3"


def _touch(p: Path) -> None:
    try:
        os.utime(p, None)
    except OSError:
        pass


def cleanup_cache(startup: bool = False) -> None:
    """古いファイルを消し、合計サイズが上限を超えていれば古い順に消す。

    startup=True(起動時)は、前回の起動で途中になった並列変換の作業フォルダも消す。
    """
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
        if now - mtime > settings.max_cache_age:
            remove_path(p)
        else:
            keep.append((mtime, size, p))
    keep.sort()
    total = sum(s for _, s, _ in keep)
    while keep and total > settings.max_cache_bytes:
        _, size, p = keep.pop(0)
        remove_path(p)
        total -= size


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

    warnings: list[str] = []

    class _Logger:
        def debug(self, msg): pass
        def info(self, msg): pass
        def warning(self, msg): warnings.append(str(msg))
        def error(self, msg): pass

    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "outtmpl": str(work / "source.%(ext)s"),
        "paths": {"home": str(work), "temp": str(work)},
        "max_filesize": settings.max_filesize,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _Logger(),
        "progress_hooks": [hook],
        "socket_timeout": 30,
        "retries": 3,
        "overwrites": True,
        "cachedir": False,
    }
    opts["ffmpeg_location"] = audio.find_ffmpeg()
    try:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    raise UserError("情報を取得できませんでした。")
                if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
                    raise UserError("プレイリストには対応していません。1本の動画・曲の URL を入力してください。")
                if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                    raise UserError("ライブ配信には対応していません。")
                _check_duration(info.get("duration"))
                info = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise UserError(_translate_ytdlp_error(str(e), warnings), 502) from None

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
        meta = {"id": source_id, "title": title, "duration": duration,
                "source": "url", "url": url, "created": time.time()}
        final = sources_dir() / source_id
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        src.rename(work / ("source" + src.suffix.lower()))
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
                "source": "file", "created": time.time()}
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


_render_locks: dict[str, threading.Lock] = {}
_render_guard = threading.Condition()  # _render_locks / _renders / _foreground を守る
_renders: list[_Render] = []
_foreground = 0  # 利用者が待っている変換の数。1 つでもあれば先回り変換は動かさない
_render_sem = threading.Semaphore(2)  # 同時変換数の上限


def ensure_render(source_id: str, semitones: int, *, background: bool = False,
                  supersede: bool = False,
                  wanted: Callable[[], bool] | None = None) -> Path:
    """キーを変えた MP3 を(無ければ変換して)用意し、そのパスを返す。

    background=True は先回り変換。利用者が待っている変換があるときは始めず、
    途中で利用者の変換が来たら取り消される(どちらも audio.RenderCancelled)。
    wanted は「この先回り変換がまだ必要か」を返す関数で、不要になっていたら始めない。
    supersede=True は「この音源の、ほかのキーへの変換はもう要らない」という意味で、
    実行中のものを取り消す(スライダーを動かして別のキーに変えたとき)。
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
    me = _Render(source_id, semitones, background)
    global _foreground
    with _render_guard:
        if background:
            if _foreground or (wanted is not None and not wanted()):
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
        return out
    finally:
        with _render_guard:
            _renders.remove(me)
            if not background:
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


def cancel_all_renders() -> None:
    with _render_guard:
        for r in _renders:
            r.cancel.set()


class Prefetcher:
    """聴いているキーの隣のキーを、空いている時間に先回りして変換しておく。

    +/− を 1 つずつ動かしたときに待たずに済むようにするためのもの。
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


def schedule_prefetch(source_id: str, semitones: int) -> None:
    """いま聴いているキーの隣(動かしている向きを先に)を先回り変換の対象にする。"""
    if not settings.prefetch:
        return
    prev = _last_key.get(source_id)
    if len(_last_key) > 64:
        _last_key.clear()
    _last_key[source_id] = semitones
    step = -1 if prev is not None and semitones < prev else 1
    keys = [k for k in (semitones + step, semitones - step)
            if audio.MIN_SEMITONES <= k <= audio.MAX_SEMITONES
            and not render_path(source_id, k).exists()]
    prefetcher.schedule(source_id, keys)


def safe_filename(title: str, semitones: int) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title or "audio").strip(" .")
    t = re.sub(r"\s+", " ", t)[:80].strip(" .") or "audio"
    key = "±0" if semitones == 0 else f"{semitones:+d}"
    return f"{t} ({key}).mp3"


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


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _run_job(job: Job, url: str) -> None:
    def progress(p: float) -> None:
        job.progress = p
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
    try:
        cleanup_cache()
    except OSError:
        pass


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
    yield
    # 終了時: 動いている変換(ffmpeg)を止め、作業ファイルを片付け終わるのを少し待つ
    prefetcher.stop()
    cancel_all_renders()
    with _render_guard:
        _render_guard.wait_for(lambda: not _renders, timeout=5)


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
        return JSONResponse({"detail": "このアドレスからは利用できません。http://127.0.0.1:8000 を開いてください。"},
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
def api_job(job_id: str):
    if not JOB_RE.fullmatch(job_id):
        raise UserError("ジョブが見つかりません。", 404)
    job = _jobs.get(job_id)
    if not job:
        raise UserError("ジョブが見つかりません。", 404)
    return {"status": job.status, "progress": job.progress, "result": job.result, "error": job.error}


@app.post("/api/upload")
def api_upload(file: UploadFile = File(...)):
    meta = save_upload(file)
    try:
        cleanup_cache()
    except OSError:
        pass
    return meta


def _audio_url(source_id: str, semitones: int, download: bool = False) -> str:
    u = f"/api/audio/{source_id}?semitones={semitones}"
    return u + "&download=1" if download else u


@app.post("/api/render")
def api_render(req: RenderRequest):
    check_id(req.id)
    ensure_render(req.id, req.semitones, supersede=True)
    schedule_prefetch(req.id, req.semitones)
    meta = read_meta(req.id) or {}
    return {"url": _audio_url(req.id, req.semitones),
            "download_url": _audio_url(req.id, req.semitones, True),
            "filename": safe_filename(meta.get("title", "audio"), req.semitones)}


@app.get("/api/audio/{source_id}")
def api_audio(source_id: str, semitones: int = Query(0, ge=-12, le=12), download: int = 0):
    check_id(source_id)
    path = ensure_render(source_id, semitones)
    meta = read_meta(source_id) or {}
    name = safe_filename(meta.get("title", "audio"), semitones)
    disp = "attachment" if download else "inline"
    headers = {"Content-Disposition": f"{disp}; filename=\"{_ascii_name(name)}\"; filename*=UTF-8''{quote(name)}",
               "Cache-Control": "no-cache"}
    return FileResponse(path, media_type="audio/mpeg", headers=headers)


def _ascii_name(name: str) -> str:
    s = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return s


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ---------------------------------------------------------------- 起動
def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="キー変更アプリ")
    ap.add_argument("--host", default=os.environ.get("KEYCHANGER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("KEYCHANGER_PORT", "8000")))
    ap.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = ap.parse_args()

    if args.host not in ("0.0.0.0", "::"):
        ALLOWED_HOSTS.add(args.host.strip("[]").lower())
    else:
        print("[注意] すべてのネットワークに公開しています。KEYCHANGER_ALLOWED_HOSTS に"
              "この PC のアドレスを指定しないと、他の端末からは開けません。")
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 50)
    print(" キー変更アプリを起動しました")
    print(f" ブラウザで {url} を開いてください")
    print(" 終了するにはこの画面で Ctrl + C を押すか、ウィンドウを閉じます")
    print("=" * 50)
    if not audio.find_ffmpeg():
        print("[注意] ffmpeg が見つかりません。README の手順でインストールしてください。")
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
