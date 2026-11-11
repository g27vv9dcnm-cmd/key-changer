"""ffmpeg を使った音声処理(キー変更・長さ取得)。

ffmpeg は常に引数リストで呼び出し、shell=True は使いません。
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

MIN_SEMITONES = -12
MAX_SEMITONES = 12
WORK_RATE = 44100  # 処理用のサンプルレート
MP3_BITRATE = "192k"


HOMEBREW_FFMPEG_FULL = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",  # Apple シリコン
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",     # Intel Mac
)


class FFmpegMissing(RuntimeError):
    pass


class AudioError(RuntimeError):
    pass


def find_ffmpeg() -> str | None:
    """ffmpeg 実行ファイルのパスを返す(見つからなければ None)。"""
    env = os.environ.get("KEYCHANGER_FFMPEG")
    if env and Path(env).is_file():
        return env
    # Mac の Homebrew「ffmpeg-full」(rubberband 入り)は PATH に入らないことがあるので直接探す
    for keg in HOMEBREW_FFMPEG_FULL:
        if Path(keg).is_file():
            return keg
    return shutil.which("ffmpeg")


def require_ffmpeg() -> str:
    path = find_ffmpeg()
    if not path:
        raise FFmpegMissing("ffmpeg が見つかりません")
    return path


@functools.lru_cache(maxsize=8)
def _filters_of(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def has_rubberband(ffmpeg: str | None = None) -> bool:
    """ffmpeg に rubberband フィルタが組み込まれているか。"""
    if os.environ.get("KEYCHANGER_NO_RUBBERBAND") == "1":
        return False
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False
    return re.search(r"^\s*\S+\s+rubberband\s", _filters_of(ffmpeg), re.M) is not None


def validate_semitones(n: int) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError("semitones must be int")
    if n < MIN_SEMITONES or n > MAX_SEMITONES:
        raise ValueError("semitones out of range")
    return n


def pitch_ratio(semitones: int) -> float:
    return 2.0 ** (semitones / 12.0)


def _atempo_chain(factor: float) -> list[str]:
    """atempo は古い ffmpeg だと 0.5〜2.0 の範囲しか受け付けないので分割する。"""
    parts: list[str] = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.10f}")
    return parts


def build_filter(semitones: int, engine: str) -> str:
    """-af に渡すフィルタ文字列を作る。engine は "rubberband" か "fallback"。"""
    validate_semitones(semitones)
    chain = [f"aresample={WORK_RATE}"]
    if semitones != 0:
        ratio = pitch_ratio(semitones)
        if engine == "rubberband":
            chain.append(f"rubberband=pitch={ratio:.10f}:tempo=1:pitchq=quality")
        elif engine == "fallback":
            # 再生速度を変えて音程を上げ下げし、atempo で長さを元に戻す
            chain.append(f"asetrate={WORK_RATE * ratio:.4f}")
            chain.append(f"aresample={WORK_RATE}")
            chain.extend(_atempo_chain(1.0 / ratio))
        else:
            raise ValueError(f"unknown engine: {engine}")
    return ",".join(chain)


def choose_engine(ffmpeg: str | None = None) -> str:
    return "rubberband" if has_rubberband(ffmpeg) else "fallback"


def render(src: Path, dst: Path, semitones: int, engine: str | None = None,
           timeout: float = 900) -> str:
    """src のキーを semitones 半音ずらして MP3 で dst に書き出す。使った engine を返す。"""
    ffmpeg = require_ffmpeg()
    engine = engine or choose_engine(ffmpeg)
    af = build_filter(semitones, engine)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-sn", "-dn",
        "-af", af,
        "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
        "-f", "mp3", str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        tmp.unlink(missing_ok=True)
        raise AudioError("変換がタイムアウトしました") from e
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise AudioError("変換に失敗しました: " + (proc.stderr or "").strip()[-500:])
    os.replace(tmp, dst)
    return engine


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe(src: Path) -> dict:
    """ffmpeg -i の出力から長さと音声ストリームの有無を調べる(ffprobe 不要)。"""
    ffmpeg = require_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    err = proc.stderr or ""
    has_audio = re.search(r"Stream #\S+.*: Audio:", err) is not None
    m = _DUR_RE.search(err)
    duration = None
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    return {"has_audio": has_audio, "duration": duration}
