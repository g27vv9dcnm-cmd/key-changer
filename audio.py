"""ffmpeg を使った音声処理(キー変更・長さ取得)。

ffmpeg は常に引数リストで呼び出し、shell=True は使いません。

キー変更(rubberband)は 1 コアしか使わず時間がかかるので、長い音声は
「区切って並列に変換 → つなぎ合わせる」方法で変換します(_render_parallel)。

変換結果は WAV(32bit float)で書き出します。MP3 へのエンコードは 1 コアしか使えず、
並列変換のあとに残る時間の大半を占めるので、再生には使いません。MP3 は保存する
ときにだけ、この WAV から作ります(encode_mp3)。
"""
from __future__ import annotations

import array
import functools
import json
import math
import os
import queue
import re
import shutil
import struct
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
MP3_BITRATE = "192k"
# 変換結果(WAV)は 32bit float のまま保存する。キーを変えた音はフルスケール(±1.0)を少し超える山が
# できやすく、16bit にするとそこが切り詰められて歪む。float のままなら、変換結果をそのまま MP3 に
# していたときと同じ音を、再生にも保存にも使える。
PCM_BYTES = 4      # 変換結果(WAV)の 1 サンプルのバイト数(32bit float)
WAV_HEADER_BYTES = 58


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


def _wav_header(channels: int, data_bytes: int) -> bytes:
    """32bit float の WAV ファイルの先頭 58 バイト(float の WAV に要る fact チャンク付き)。"""
    block = channels * PCM_BYTES
    fmt = struct.pack("<HHIIHHH", 3, channels, WORK_RATE, WORK_RATE * block, block, 8 * PCM_BYTES, 0)
    return (b"RIFF" + struct.pack("<I", WAV_HEADER_BYTES - 8 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<II", 4, data_bytes // block)
            + b"data" + struct.pack("<I", data_bytes))


def render(src: Path, dst: Path, semitones: int, engine: str | None = None,
           timeout: float = 900, *, jobs: int | None = None,
           cancel: threading.Event | None = None) -> str:
    """src のキーを semitones 半音ずらし、WAV(32bit float)で dst に書き出す。使った engine を返す。

    semitones が 0 のときは WAV に直すだけ。
    jobs は並列変換に使うプロセス数(None = CPU のコア数、1 = 並列にしない)。
    cancel がセットされると途中でやめて RenderCancelled を送出する。
    """
    ffmpeg = require_ffmpeg()
    engine = engine or choose_engine(ffmpeg)
    build_filter(semitones, engine)  # 引数の検証
    dst.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    jobs = default_jobs() if jobs is None else max(1, int(jobs))
    info = probe(src)
    channels = 1 if info["channels"] == 1 else 2
    if semitones != 0 and engine == "rubberband" and jobs > 1:
        try:
            if _render_parallel(ffmpeg, src, dst, semitones, jobs, deadline, cancel,
                                info["duration"], channels):
                return engine
        except AudioError:
            # 並列変換でうまくいかなかったら、従来どおり 1 本で変換し直す
            if time.monotonic() > deadline:
                raise
    _render_single(ffmpeg, src, dst, semitones, engine, deadline, cancel, channels)
    return engine


def _run_to_file(cmd: list[str], dst: Path, deadline: float, cancel: threading.Event | None,
                 what: str) -> None:
    """cmd(最後の引数に書き出し先を足して実行する)の結果を dst に置く。途中のファイルは残さない。"""
    tmp = dst.with_name(dst.name + ".part")
    try:
        code, err = _run_ffmpeg([*cmd, str(tmp)], deadline, cancel)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"{what}に失敗しました: " + err.strip()[-500:])
    os.replace(tmp, dst)


def _render_single(ffmpeg: str, src: Path, dst: Path, semitones: int, engine: str,
                   deadline: float, cancel: threading.Event | None, channels: int) -> None:
    """ffmpeg 1 本で最初から最後まで変換する。"""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-sn", "-dn", "-map_metadata", "-1",
        "-af", build_filter(semitones, engine),
        "-ac", str(channels), "-c:a", "pcm_f32le", "-f", "wav",
    ]
    _run_to_file(cmd, dst, deadline, cancel, "変換")


def encode_mp3(src: Path, dst: Path, *, tags_from: Path | None = None, timeout: float = 900,
               cancel: threading.Event | None = None) -> None:
    """src(音源、または render で作った WAV)を MP3 にして dst に書き出す。

    tags_from を渡すと、曲名などのタグをそのファイルから写す(WAV にはタグが無いので、元の音源を渡す)。
    """
    ffmpeg = require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src)]
    if tags_from is not None:
        cmd += ["-i", str(tags_from), "-map", "0:a", "-map_metadata", "1"]
    else:
        cmd += ["-vn", "-sn", "-dn"]
    cmd += ["-af", build_filter(0, "fallback"), *_encode_args()]
    _run_to_file(cmd, dst, time.monotonic() + timeout, cancel, "MP3 への変換")


# ---------------------------------------------------------------- 並列変換
#
# 1. 音源を 1 回だけ PCM(32bit float)にデコードする
# 2. 少しずつ重なるように区切り、区間ごとに ffmpeg(rubberband)を並列に走らせる
#    (区間の変換結果は 32bit float の PCM。そのまま WAV の中身になる)
# 3. 隣り合う区間の重なり(1 秒)の中から、2 つの波形が一番よく一致する場所を探し、
#    そこで短くクロスフェードしてつなぐ(一致している場所でつなぐので、つなぎ目は聞こえない)
# 4. つないだ PCM を、できた順に WAV ファイルへ書き足していく

MAX_JOBS = 16
SEGMENTS_PER_JOB = 1          # 区間の数 ≒ jobs × これ。1 = 各プロセスに 1 区間ずつ。細かく切ると、重ねて
                              # 変換する分と ffmpeg の起動が増えて遅くなる(12 スレッドの PC で実測)
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


def _samples(buf: bytes) -> array.array:
    """32bit float の PCM(リトルエンディアン)のバイト列を数の配列にする。"""
    a = array.array("f")
    a.frombytes(buf)
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _mono(buf: bytes, channels: int) -> list[float]:
    """PCM を、つなぐ場所を探すための 1 チャンネルの波形(左右の和)にする。"""
    a = _samples(buf)
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
    """a から b へなめらかに切り替える(a, b は同じ長さの 32bit float の PCM)。

    2 つの波形がよく一致している(rho が 1 に近い)ときは普通の直線フェード、
    一致していないときは音量が一瞬下がらないよう、その分だけ持ち上げる。
    """
    xa, xb = _samples(a), _samples(b)
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


# find_seam は Python だけで書いてあり、1 回に数十ミリ秒かかる。同じプロセスの中では同時に
# 1 つしか計算できない(GIL)ので、コアの多い PC では ffmpeg の並列変換よりもここが遅れて、
# 変換全体の待ち時間を延ばしてしまう。そこで、別の Python プロセスに計算を頼めるようにする。
SEAM_HELPERS_MAX = 4


def seam_helpers(jobs: int) -> int:
    """つなぐ場所の計算を頼む別プロセスの数。0 のときは自分のプロセスの中で計算する。

    並列数が少ないときは、区間の変換を待っている間に計算が終わるので、別プロセスは使わない。
    """
    n = min(SEAM_HELPERS_MAX, jobs // 3)
    return n if n >= 2 and sys.executable else 0


class _HelperFailed(Exception):
    """別プロセスでの計算がうまくいかなかった(自分のプロセスで計算し直す)。"""


class _SeamHelper:
    """つなぐ場所の計算(find_seam)を引き受ける別プロセス 1 つ。

    このファイル自身を「--seam-helper」付きで起動する(_seam_helper_main)。
    頼みごとと答えは、1 行ずつの JSON でやり取りする。
    """

    def __init__(self) -> None:
        # -I -S: 環境変数や site-packages の影響を受けず、標準ライブラリだけで素早く起動する
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--seam-helper"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._answers: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                self._answers.put(line)
        except (OSError, ValueError):
            pass
        self._answers.put(None)  # もう答えは来ない

    def ask(self, job: dict, check) -> tuple[int, int, float]:
        """job(_seam_job の引数)を計算させて答えを返す。待っている間、check() を呼び続ける。"""
        try:
            self.proc.stdin.write(json.dumps(job).encode("ascii") + b"\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise _HelperFailed(str(e)) from None
        while True:
            check()
            try:
                line = self._answers.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                p, lag, rho = json.loads(line)  # line が None(終了)や {"error": ...} ならここで失敗する
                return int(p), int(lag), float(rho)
            except (TypeError, ValueError) as e:
                raise _HelperFailed(str(e)) from None

    def close(self) -> None:
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            _kill(self.proc)


def _seam_job(a: str, a_off: int, b: str, b_off: int, channels: int) -> tuple[int, int, float]:
    """区間のファイル a, b の重なり(それぞれ a_off, b_off フレーム目から)を読んで、つなぐ場所を探す。"""
    fb = PCM_BYTES * channels
    x = _read_frames(Path(a), a_off, OVERLAP, fb)
    y = _read_frames(Path(b), b_off, OVERLAP + 2 * MAX_LAG, fb)
    return find_seam(_mono(x, channels), _mono(y, channels))


def _seam_helper_main() -> None:
    """別プロセスとして動く側。標準入力から 1 行ずつ頼みごとを受け取り、答えを標準出力に返す。"""
    for line in sys.stdin.buffer:
        try:
            answer = list(_seam_job(**json.loads(line)))
        except Exception as e:  # noqa: BLE001  (頼んだ側が、自分で計算し直す)
            answer = {"error": str(e)}
        sys.stdout.buffer.write(json.dumps(answer).encode("ascii") + b"\n")
        sys.stdout.buffer.flush()


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
                     deadline: float, cancel: threading.Event | None,
                     duration: float | None, channels: int) -> bool:
    """区切って並列に変換する。短くて並列にする意味が無いときは何もせず False を返す。"""
    seg_len = segment_frames(duration, jobs)
    if seg_len is None:
        return False
    fin = 4 * channels          # デコードした音源(32bit float)の 1 フレームのバイト数
    fb = PCM_BYTES * channels   # 変換結果(32bit float)の 1 フレームのバイト数
    base = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    raw = ["-f", "f32le", "-ar", str(WORK_RATE), "-ac", str(channels)]
    af = f"rubberband=pitch={pitch_ratio(semitones):.10f}:tempo=1:pitchq=quality"
    work = Path(tempfile.mkdtemp(prefix=dst.name + ".", suffix=".part", dir=dst.parent))
    pcm = work / "source.f32"
    tmp = work / "out.wav"
    stop = threading.Event()  # どこかで失敗・中止したら全部止める
    n_helpers = seam_helpers(jobs)
    pool = ThreadPoolExecutor(max_workers=jobs)                  # 区間の変換
    seam_pool = ThreadPoolExecutor(max_workers=max(1, n_helpers))  # つなぐ場所の計算
    ready: queue.Queue = queue.Queue()                # できあがった順に並べる (区間ファイル, 変換, つなぎ目)
    procs: list[subprocess.Popen] = []
    helpers: list[_SeamHelper] = []                   # つなぐ場所の計算を頼む別プロセス
    idle: queue.Queue = queue.Queue()                 # そのうち、手が空いているもの

    def check() -> None:
        if _stopped(cancel, stop):
            raise RenderCancelled()
        if time.monotonic() > deadline:
            raise AudioError("変換がタイムアウトしました")

    def seg_path(i: int) -> Path:
        return work / f"seg{i}.pcm"

    def run_segment(i: int, start: int, end: int) -> None:
        """元の音声の [start, end) フレームを変換して seg_path(i) に書く。"""
        check()
        cmd = [*base, *raw, "-ss", f"{start / WORK_RATE:.6f}",
               "-t", f"{(end - start) / WORK_RATE:.6f}", "-i", str(pcm),
               "-af", af, "-f", "f32le", str(seg_path(i))]
        code, err = _run_ffmpeg(cmd, deadline, cancel, stop)
        if code != 0 or not seg_path(i).exists():
            raise AudioError("変換に失敗しました: " + err.strip()[-500:])

    def locate(k: int, a_off: int, b_off: int, a: bytes, b: bytes) -> tuple[int, int, float]:
        """区間 k と k+1 の重なり a, b から、つなぐ場所を探す。手の空いた別プロセスがあれば、そちらに頼む。"""
        try:
            helper = idle.get_nowait()
        except queue.Empty:
            helper = None
        if helper is not None:
            try:
                found = helper.ask({"a": str(seg_path(k)), "a_off": a_off, "b": str(seg_path(k + 1)),
                                    "b_off": b_off, "channels": channels}, check)
                idle.put(helper)
                return found
            except _HelperFailed:
                helper.close()  # このあとは使わない。今回の分は自分で計算する
        return find_seam(_mono(a, channels), _mono(b, channels))

    def seam(k: int, left, right, a_off: int, b_off: int) -> tuple[int, int, bytes]:
        """区間 k と k+1 をつなぐ場所を決める。a_off / b_off は重なりの先頭の、各区間の中での位置。"""
        left.result()
        right.result()
        check()
        a = _read_frames(seg_path(k), a_off, OVERLAP, fb)
        b = _read_frames(seg_path(k + 1), b_off - MAX_LAG, OVERLAP + 2 * MAX_LAG, fb)
        p, lag, rho = locate(k, a_off, b_off - MAX_LAG, a, b)
        q = MAX_LAG + p + lag
        xf = crossfade(a[p * fb:(p + XFADE) * fb], b[q * fb:(q + XFADE) * fb], channels, rho)
        # (区間 k を使う終わり, 区間 k+1 を使い始める位置, つなぎ目の PCM)
        return a_off + p, b_off + p + lag + XFADE, xf

    def plan() -> None:
        """音源を PCM にデコードしながら、デコードが済んだ所から順に区間の変換を始める。"""
        err_file = tempfile.TemporaryFile()
        try:
            dec = subprocess.Popen(
                [*base, "-i", str(src), "-vn", "-sn", "-dn", "-af", f"aresample={WORK_RATE}",
                 *raw, str(pcm)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err_file)
            procs.append(dec)
            tail_min = seg_len // 2   # 最後の区間がこれより短くならないようにする
            i, start, prev, prev_start = 0, 0, None, 0
            while True:
                check()
                done = dec.poll() is not None
                size = _file_size(pcm) // fin
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
        except BaseException as e:  # noqa: BLE001  (書き足す側に伝える)
            ready.put(e)
        finally:
            err_file.close()

    planner = threading.Thread(target=plan, daemon=True)
    try:
        planner.start()
        for _ in range(n_helpers):  # 最初の区間を変換している間に起動が済む
            try:
                helpers.append(_SeamHelper())
            except OSError:
                break
            idle.put(helpers[-1])
        try:
            with open(tmp, "wb") as out:
                out.write(_wav_header(channels, 0))  # 大きさは最後に書き直す
                written = 0
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
                    # できた分を WAV に書き足す
                    with open(seg_path(i), "rb") as f:
                        f.seek(take * fb)
                        left = (end - take) * fb
                        while left > 0:
                            check()
                            chunk = f.read(min(left, 1 << 20))
                            if not chunk:
                                raise AudioError("区間の変換結果が短すぎます")
                            out.write(chunk)
                            left -= len(chunk)
                    out.write(xf)
                    written += (end - take) * fb + len(xf)
                    seg_path(i).unlink(missing_ok=True)
                    if seam_fut is None:
                        break
                    take = next_take
                if written == 0 or written > 0xFFFFFFFF - WAV_HEADER_BYTES:
                    raise AudioError("変換結果の大きさが正しくありません")
                out.seek(0)
                out.write(_wav_header(channels, written))
        except OSError as e:  # ディスクがいっぱい、など
            raise AudioError(f"変換結果を書き出せませんでした: {e}") from e
        os.replace(tmp, dst)
        return True
    finally:
        stop.set()
        if planner.is_alive():
            planner.join()
        pool.shutdown(wait=True, cancel_futures=True)
        seam_pool.shutdown(wait=True, cancel_futures=True)
        for proc in procs:
            if proc.poll() is None:
                _kill(proc)
        for helper in helpers:
            helper.close()
        shutil.rmtree(work, ignore_errors=True)


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_START_RE = re.compile(r"Duration:[^\n]*?\bstart:\s*(-?\d+(?:\.\d+)?)")
_FORMAT_RE = re.compile(r"^Input #0, ([^\n]+?), from ", re.M)
_AUDIO_RE = re.compile(r"^\s*Stream #[^\n]*?: Audio: (\w+)(?: \(([^)/\n]*)\))?[^\n]*", re.M)
_VIDEO_RE = re.compile(r"^\s*Stream #[^\n]*?: Video: [^\n]*", re.M)
_CH_RE = re.compile(r"\d+ Hz, ([^,\n]+)")


def probe(src: Path) -> dict:
    """ffmpeg -i の出力から、音声ファイルの中身を調べる(ffprobe 不要)。

    has_audio  音声が入っているか
    duration   長さ(秒)。分からないときは None
    channels   1(モノラル)か 2(それ以外。分からないときも 2)
    以下は「ブラウザでそのまま再生できる音声か」を判断するための情報(最初の音声について):
    format     入れ物の種類("mov,mp4,m4a,3gp,3g2,mj2" など ffmpeg の呼び名)
    codec      音声の形式("aac" "opus" "pcm_s16le" など)
    profile    形式の細かい種類("LC" など。無ければ None)
    layout     チャンネルの並び("stereo" "mono" "5.1" など。分からなければ None)
    start      音声が始まる時刻(秒)。分からなければ None
    audio_streams  音声の本数
    video      映像が入っているか(ジャケット画像は数えない)
    """
    ffmpeg = require_ffmpeg()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", str(src)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    err = proc.stderr or ""
    streams = list(_AUDIO_RE.finditer(err))
    codec = profile = layout = None
    if streams:
        codec, profile = streams[0].group(1), streams[0].group(2)
        m = _CH_RE.search(streams[0].group(0))
        layout = m.group(1).strip() if m else None
    channels = 1 if layout in ("mono", "1 channels") else 2
    m = _DUR_RE.search(err)
    duration = None
    if m:
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    m = _START_RE.search(err)
    start = float(m.group(1)) if m else None
    m = _FORMAT_RE.search(err)
    return {
        "has_audio": bool(streams), "duration": duration, "channels": channels,
        "format": m.group(1).strip() if m else None, "codec": codec, "profile": profile,
        "layout": layout, "start": start, "audio_streams": len(streams),
        "video": any("(attached pic)" not in v.group(0) for v in _VIDEO_RE.finditer(err)),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["--seam-helper"]:
        _seam_helper_main()
