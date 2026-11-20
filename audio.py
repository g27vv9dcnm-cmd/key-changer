"""ffmpeg を使った音声処理(キー変更・長さ取得)。

ffmpeg は常に引数リストで呼び出し、shell=True は使いません。

キー変更(rubberband)は 1 コアしか使わず時間がかかるので、長い音声は
「区切って並列に変換 → つなぎ合わせて MP3 化」します(_render_parallel)。
"""
from __future__ import annotations

import array
import functools
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import accumulate
from operator import add, mul
from pathlib import Path

MIN_SEMITONES = -12
MAX_SEMITONES = 12
WORK_RATE = 44100  # 処理用のサンプルレート
MP3_BITRATE = "320k"  # MP3 の最高ビットレート(劣化が一番少ない)

# rubberband の設定(音質重視。速さはほとんど変わらない)
#   pitchq=quality     : 音程を変えるときの計算を高品質にする
#   formant=preserved  : 声の響き(フォルマント)を保つ。キーを上げても甲高い声、下げてもこもった声にならない
#   channels=together  : 左右をまとめて処理し、音の広がり(定位)が崩れないようにする
RUBBERBAND_OPTS = "tempo=1:pitchq=quality:formant=preserved:channels=together"
RENDER_VERSION = "q2"  # 変換結果の音質が変わったら上げる(キャッシュのファイル名に入る)


HOMEBREW_FFMPEG_FULL = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",  # Apple シリコン
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",     # Intel Mac
)


class FFmpegMissing(RuntimeError):
    pass


class AudioError(RuntimeError):
    pass


class RenderCancelled(Exception):
    """変換が途中で取り消された(cancel イベントがセットされた)。"""


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


@functools.lru_cache(maxsize=8)
def _version_of(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def has_soxr(ffmpeg: str | None = None) -> bool:
    """ffmpeg に高音質なサンプルレート変換(soxr)が組み込まれているか。"""
    if os.environ.get("KEYCHANGER_NO_SOXR") == "1":
        return False
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False
    return "--enable-libsoxr" in _version_of(ffmpeg)


def resample_filter(ffmpeg: str | None = None) -> str:
    """処理用のサンプルレートにそろえるフィルタ。soxr があれば高音質な方式を使う。"""
    if has_soxr(ffmpeg):
        return f"aresample={WORK_RATE}:resampler=soxr:precision=28"
    return f"aresample={WORK_RATE}"


def rubberband_filter(semitones: int) -> str:
    return f"rubberband=pitch={pitch_ratio(semitones):.10f}:{RUBBERBAND_OPTS}"


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


def build_filter(semitones: int, engine: str, ffmpeg: str | None = None) -> str:
    """-af に渡すフィルタ文字列を作る。engine は "rubberband" か "fallback"。"""
    validate_semitones(semitones)
    resample = resample_filter(ffmpeg)
    chain = [resample]
    if semitones != 0:
        ratio = pitch_ratio(semitones)
        if engine == "rubberband":
            chain.append(rubberband_filter(semitones))
        elif engine == "fallback":
            # 再生速度を変えて音程を上げ下げし、atempo で長さを元に戻す
            chain.append(f"asetrate={WORK_RATE * ratio:.4f}")
            chain.append(resample)
            chain.extend(_atempo_chain(1.0 / ratio))
        else:
            raise ValueError(f"unknown engine: {engine}")
    return ",".join(chain)


def choose_engine(ffmpeg: str | None = None) -> str:
    return "rubberband" if has_rubberband(ffmpeg) else "fallback"


def _stopped(*events: threading.Event | None) -> bool:
    return any(e is not None and e.is_set() for e in events)


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_ffmpeg(cmd: list[str], deadline: float, cancel: threading.Event | None = None,
                stop: threading.Event | None = None) -> tuple[int, str]:
    """ffmpeg を実行して (終了コード, エラー出力) を返す。

    cancel / stop がセットされたら ffmpeg を止めて RenderCancelled、
    deadline(time.monotonic の値)を過ぎたら AudioError を送出する。
    """
    if _stopped(cancel, stop):
        raise RenderCancelled()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if _stopped(cancel, stop):
                    raise RenderCancelled() from None
                if time.monotonic() > deadline:
                    raise AudioError("変換がタイムアウトしました") from None
    finally:
        if proc.poll() is None:
            _kill(proc)
    return proc.returncode, (err or b"").decode("utf-8", "replace")


def _encode_args() -> list[str]:
    return ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3"]


def render(src: Path, dst: Path, semitones: int, engine: str | None = None,
           timeout: float = 900, *, jobs: int | None = None,
           cancel: threading.Event | None = None) -> str:
    """src のキーを semitones 半音ずらして MP3 で dst に書き出す。使った engine を返す。

    jobs は並列変換に使うプロセス数(None = CPU のコア数、1 = 並列にしない)。
    cancel がセットされると途中でやめて RenderCancelled を送出する。
    """
    ffmpeg = require_ffmpeg()
    engine = engine or choose_engine(ffmpeg)
    build_filter(semitones, engine, ffmpeg)  # 引数の検証
    dst.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    jobs = default_jobs() if jobs is None else max(1, int(jobs))
    if semitones != 0 and engine == "rubberband" and jobs > 1:
        try:
            if _render_parallel(ffmpeg, src, dst, semitones, jobs, deadline, cancel):
                return engine
        except AudioError:
            # 並列変換でうまくいかなかったら、従来どおり 1 本で変換し直す
            if time.monotonic() > deadline:
                raise
    _render_single(ffmpeg, src, dst, semitones, engine, deadline, cancel)
    return engine


def _render_single(ffmpeg: str, src: Path, dst: Path, semitones: int, engine: str,
                   deadline: float, cancel: threading.Event | None) -> None:
    """ffmpeg 1 本で最初から最後まで変換する。"""
    tmp = dst.with_name(dst.name + ".part")
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-sn", "-dn",
        "-af", build_filter(semitones, engine, ffmpeg),
        *_encode_args(), str(tmp),
    ]
    try:
        code, err = _run_ffmpeg(cmd, deadline, cancel)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise AudioError("変換に失敗しました: " + err.strip()[-500:])
    os.replace(tmp, dst)


# ---------------------------------------------------------------- 並列変換
#
# 1. 音源を 1 回だけ PCM(32bit float)にデコードする
# 2. 少しずつ重なるように区切り、区間ごとに ffmpeg(rubberband)を並列に走らせる
# 3. 隣り合う区間の重なり(1 秒)の中から、2 つの波形が一番よく一致する場所を探し、
#    そこで短くクロスフェードしてつなぐ(一致している場所でつなぐので、つなぎ目は聞こえない)
# 4. つないだ PCM を、できた順に MP3 エンコーダーへ流し込む(変換とエンコードが同時に進む)

MAX_JOBS = 16
SEGMENTS_PER_JOB = 4          # 区間の数 ≒ jobs × これ(エンコードを早く始められるよう細かめに切る)
SEGMENT_MIN_SEC = 10.0        # 1 区間の最短の長さ。これの 2 倍より短い音声は並列にしない
OVERLAP = WORK_RATE           # 隣の区間と重ねる長さ(つなぐ場所を探す範囲)= 1 秒
PAD = WORK_RATE // 2          # 重なりの手前に付ける助走(rubberband の出だしを捨てる)
POST = WORK_RATE // 4         # 重なりの後ろに付ける余白
XFADE = 1024                  # クロスフェードの長さ(約 23 ミリ秒)
MAX_LAG = 128                 # つなぐときに許す時間のずれ(±約 3 ミリ秒)
_DECIM = 4                    # つなぐ場所を粗く探すときの間引き
_GRID = WORK_RATE // 100      # 区切り位置は 10 ミリ秒単位(-ss に渡す秒数がぴったり表せる)

_sumprod = getattr(math, "sumprod", None) or (lambda a, b: sum(map(mul, a, b)))


def default_jobs() -> int:
    """並列変換に使うプロセス数(環境変数 KEYCHANGER_JOBS、無ければ使える CPU コア数)。"""
    n = 0
    try:
        n = int(os.environ.get("KEYCHANGER_JOBS", "").strip() or 0)
    except ValueError:
        pass
    if n <= 0:
        try:
            n = len(os.sched_getaffinity(0))  # Linux: このプロセスが使えるコア数
        except (AttributeError, OSError):
            n = os.cpu_count() or 1
    return max(1, min(n, MAX_JOBS))


def _mono(buf: bytes, channels: int) -> list[float]:
    a = array.array("f")
    a.frombytes(buf)
    if sys.byteorder == "big":
        a.byteswap()
    if channels == 1:
        return a.tolist()
    return list(map(add, a[0::2], a[1::2]))


def _energy_prefix(x: list[float]) -> list[float]:
    return [0.0, *accumulate(map(mul, x, x))]


def find_seam(a: list[float], b: list[float]) -> tuple[int, int, float]:
    """2 つの波形の重なりから、つなぐのに一番良い場所を探す。

    a は前の区間、b は次の区間の同じ範囲(b は前後に MAX_LAG ずつ長い。b[MAX_LAG + i] が a[i] に対応)。
    戻り値は (位置 p, ずれ lag, 相関 rho)。a[p:p+XFADE] と b[MAX_LAG+p+lag:...] の差が最も小さい。
    """
    need = XFADE + 2 * MAX_LAG
    if len(a) < need or len(b) < len(a) + 2 * MAX_LAG:
        raise AudioError("重なりが短すぎます")
    d, w, ml = _DECIM, XFADE // _DECIM, MAX_LAG // _DECIM
    # 粗い探索: 4 サンプルごとの和に間引いて、位置(10 ミリ秒刻み)× ずれを総当たり
    ad = [sum(t) for t in zip(*[iter(a)] * d)]
    bd = [sum(t) for t in zip(*[iter(b)] * d)]
    ea, eb = _energy_prefix(ad), _energy_prefix(bd)
    cands: list[tuple[float, int, int]] = []
    for p in range(0, len(ad) - w + 1, _GRID // d):
        xa = ad[p:p + w]
        e_a = ea[p + w] - ea[p]
        best = None
        for lag in range(-ml, ml + 1):
            q = p + ml + lag
            e = e_a + eb[q + w] - eb[q] - 2.0 * _sumprod(xa, bd[q:q + w])
            if best is None or e < best[0]:
                best = (e, p, lag)
        cands.append(best)
    cands.sort()
    # 細かい探索: 良かった候補のまわりを元の細かさで調べ直す
    fa, fb = _energy_prefix(a), _energy_prefix(b)
    best_full = None
    for _, pd, lagd in cands[:4]:
        p = pd * d
        xa = a[p:p + XFADE]
        e_a = fa[p + XFADE] - fa[p]
        for lag in range(max(-MAX_LAG, lagd * d - d), min(MAX_LAG, lagd * d + d) + 1):
            q = p + MAX_LAG + lag
            dot = _sumprod(xa, b[q:q + XFADE])
            e_b = fb[q + XFADE] - fb[q]
            e = e_a + e_b - 2.0 * dot
            if best_full is None or e < best_full[0]:
                best_full = (e, p, lag, dot, e_a, e_b)
    _, p, lag, dot, e_a, e_b = best_full
    den = math.sqrt(e_a * e_b)
    rho = dot / den if den > 1e-12 else 1.0
    return p, lag, max(0.0, min(1.0, rho))


def crossfade(a: bytes, b: bytes, channels: int, rho: float) -> bytes:
    """a から b へなめらかに切り替える(a, b は同じ長さの f32le PCM)。

    2 つの波形がよく一致している(rho が 1 に近い)ときは普通の直線フェード、
    一致していないときは音量が一瞬下がらないよう、その分だけ持ち上げる。
    """
    xa, xb = array.array("f"), array.array("f")
    xa.frombytes(a)
    xb.frombytes(b)
    if sys.byteorder == "big":
        xa.byteswap()
        xb.byteswap()
    n = len(xa) // channels
    out = array.array("f", bytes(len(a)))
    for i in range(n):
        wb = (i + 0.5) / n
        wa = 1.0 - wb
        g = 1.0 / math.sqrt(wa * wa + wb * wb + 2.0 * rho * wa * wb)
        wa *= g
        wb *= g
        for c in range(channels):
            k = i * channels + c
            out[k] = xa[k] * wa + xb[k] * wb
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def _file_size(path: Path) -> int:
    """書き込み中のファイルの、いまの大きさ。

    Windows では書き込み中のファイルの stat が古い値を返すことがあるので、開いて末尾の位置を調べる。
    """
    try:
        with open(path, "rb") as f:
            return f.seek(0, os.SEEK_END)
    except OSError:
        return 0


def _read_frames(path: Path, start: int, count: int, frame_bytes: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start * frame_bytes)
        buf = f.read(count * frame_bytes)
    if len(buf) != count * frame_bytes:
        raise AudioError("区間の変換結果が短すぎます")
    return buf


def segment_frames(duration: float | None, jobs: int) -> int | None:
    """1 区間の長さ(フレーム数)を決める。並列にしない方が良いときは None。

    duration は音声のおおよその長さ(秒)。分からないときは None(その場合も並列にする)。
    """
    lo = max(int(SEGMENT_MIN_SEC * WORK_RATE), 2 * (OVERLAP + PAD))
    if jobs < 2:
        return None
    if not duration:
        return lo // _GRID * _GRID
    total = int(duration * WORK_RATE)
    if total < 2 * lo:
        return None
    return max(lo, total // (jobs * SEGMENTS_PER_JOB)) // _GRID * _GRID


def _render_parallel(ffmpeg: str, src: Path, dst: Path, semitones: int, jobs: int,
                     deadline: float, cancel: threading.Event | None) -> bool:
    """区切って並列に変換する。短くて並列にする意味が無いときは何もせず False を返す。"""
    info = probe(src)
    seg_len = segment_frames(info["duration"], jobs)
    if seg_len is None:
        return False
    channels = 1 if info["channels"] == 1 else 2
    fb = 4 * channels  # 1 フレームのバイト数
    base = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    raw = ["-f", "f32le", "-ar", str(WORK_RATE), "-ac", str(channels)]
    af = rubberband_filter(semitones)
    work = Path(tempfile.mkdtemp(prefix=dst.name + ".", suffix=".part", dir=dst.parent))
    pcm = work / "source.f32"
    tmp = work / "out.mp3"
    stop = threading.Event()  # どこかで失敗・中止したら全部止める
    pool = ThreadPoolExecutor(max_workers=jobs)       # 区間の変換
    seam_pool = ThreadPoolExecutor(max_workers=1)     # つなぐ場所の計算
    ready: queue.Queue = queue.Queue()                # できあがった順に並べる (区間ファイル, 変換, つなぎ目)
    procs: list[subprocess.Popen] = []

    def check() -> None:
        if _stopped(cancel, stop):
            raise RenderCancelled()
        if time.monotonic() > deadline:
            raise AudioError("変換がタイムアウトしました")

    def seg_path(i: int) -> Path:
        return work / f"seg{i}.f32"

    def run_segment(i: int, start: int, end: int) -> None:
        """元の音声の [start, end) フレームを変換して seg_path(i) に書く。"""
        check()
        cmd = [*base, *raw, "-ss", f"{start / WORK_RATE:.6f}",
               "-t", f"{(end - start) / WORK_RATE:.6f}", "-i", str(pcm),
               "-af", af, "-f", "f32le", str(seg_path(i))]
        code, err = _run_ffmpeg(cmd, deadline, cancel, stop)
        if code != 0 or not seg_path(i).exists():
            raise AudioError("変換に失敗しました: " + err.strip()[-500:])

    def seam(k: int, left, right, a_off: int, b_off: int) -> tuple[int, int, bytes]:
        """区間 k と k+1 をつなぐ場所を決める。a_off / b_off は重なりの先頭の、各区間の中での位置。"""
        left.result()
        right.result()
        check()
        a = _read_frames(seg_path(k), a_off, OVERLAP, fb)
        b = _read_frames(seg_path(k + 1), b_off - MAX_LAG, OVERLAP + 2 * MAX_LAG, fb)
        p, lag, rho = find_seam(_mono(a, channels), _mono(b, channels))
        q = MAX_LAG + p + lag
        xf = crossfade(a[p * fb:(p + XFADE) * fb], b[q * fb:(q + XFADE) * fb], channels, rho)
        # (区間 k を使う終わり, 区間 k+1 を使い始める位置, つなぎ目の PCM)
        return a_off + p, b_off + p + lag + XFADE, xf

    def plan() -> None:
        """音源を PCM にデコードしながら、デコードが済んだ所から順に区間の変換を始める。"""
        err_file = tempfile.TemporaryFile()
        try:
            dec = subprocess.Popen(
                [*base, "-i", str(src), "-vn", "-sn", "-dn", "-af", resample_filter(ffmpeg),
                 *raw, str(pcm)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_file)
            procs.append(dec)
            tail_min = seg_len // 2   # 最後の区間がこれより短くならないようにする
            i, start, prev, prev_start = 0, 0, None, 0
            while True:
                check()
                done = dec.poll() is not None
                size = _file_size(pcm) // fb
                if done and (dec.returncode != 0 or size == 0):
                    err_file.seek(0)
                    raise AudioError("デコードに失敗しました: "
                                     + err_file.read().decode("utf-8", "replace").strip()[-500:])
                while True:
                    b = (i + 1) * seg_len                       # 区間 i と i+1 の境目
                    if size >= b + tail_min:
                        end, last = b + OVERLAP // 2 + POST, False
                    elif done:
                        end, last = size, True
                    else:
                        break
                    fut = pool.submit(run_segment, i, start, end)
                    if prev is not None:
                        g0 = i * seg_len - OVERLAP // 2         # 手前の区間との重なりの先頭
                        ready.put((i - 1, prev, seam_pool.submit(
                            seam, i - 1, prev, fut, g0 - prev_start, g0 - start)))
                    if last:
                        ready.put((i, fut, None))
                        return
                    prev, prev_start = fut, start
                    start = b - OVERLAP // 2 - PAD
                    i += 1
                if not done:
                    time.sleep(0.02)
        except BaseException as e:  # noqa: BLE001  (流し込み側に伝える)
            ready.put(e)
        finally:
            err_file.close()

    planner = threading.Thread(target=plan, daemon=True)
    enc_err = tempfile.TemporaryFile()
    try:
        enc = subprocess.Popen(
            [*base, *raw, "-i", "pipe:0", "-i", str(src), "-map", "0:a", "-map_metadata", "1",
             *_encode_args(), str(tmp)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=enc_err)
        procs.append(enc)
        planner.start()
        try:
            take = 0  # いまの区間を使い始める位置
            while True:
                while True:
                    check()
                    try:
                        item = ready.get(timeout=0.1)
                        break
                    except queue.Empty:
                        pass
                if isinstance(item, BaseException):
                    raise item
                i, seg_fut, seam_fut = item
                seg_fut.result()
                if seam_fut is not None:
                    end, next_take, xf = seam_fut.result()
                else:
                    end, next_take, xf = _file_size(seg_path(i)) // fb, 0, b""
                if end < take:
                    raise AudioError("区間のつなぎ合わせに失敗しました")
                # できた分を MP3 エンコーダーへ流し込む
                with open(seg_path(i), "rb") as f:
                    f.seek(take * fb)
                    left = (end - take) * fb
                    while left > 0:
                        check()
                        chunk = f.read(min(left, 1 << 18))
                        if not chunk:
                            raise AudioError("区間の変換結果が短すぎます")
                        enc.stdin.write(chunk)
                        left -= len(chunk)
                enc.stdin.write(xf)
                seg_path(i).unlink(missing_ok=True)
                if seam_fut is None:
                    break
                take = next_take
            enc.stdin.close()
            while True:
                try:
                    enc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    check()
        except OSError as e:  # エンコーダーが途中で終了した(パイプが切れた)など
            raise AudioError(f"MP3 への変換に失敗しました: {e}") from e
        enc_err.seek(0)
        err = enc_err.read().decode("utf-8", "replace")
        if enc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise AudioError("MP3 への変換に失敗しました: " + err.strip()[-500:])
        os.replace(tmp, dst)
        return True
    finally:
        stop.set()
        if planner.is_alive():
            planner.join()
        pool.shutdown(wait=True, cancel_futures=True)
        seam_pool.shutdown(wait=True, cancel_futures=True)
        for proc in procs:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.poll() is None:
                _kill(proc)
        enc_err.close()
        shutil.rmtree(work, ignore_errors=True)


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CH_RE = re.compile(r"Stream #\S+.*: Audio:[^\n]*?\d+ Hz, ([^,\n]+)")


def probe(src: Path) -> dict:
    """ffmpeg -i の出力から長さ・音声ストリームの有無・チャンネル数を調べる(ffprobe 不要)。

    channels は 1(モノラル)か 2(それ以外。分からないときも 2)。
    """
    ffmpeg = require_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    err = proc.stderr or ""
    has_audio = re.search(r"Stream #\S+.*: Audio:", err) is not None
    m = _CH_RE.search(err)
    channels = 1 if m and m.group(1).strip() in ("mono", "1 channels") else 2
    m = _DUR_RE.search(err)
    duration = None
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    return {"has_audio": has_audio, "duration": duration, "channels": channels}
