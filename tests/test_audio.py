import array
import random
import subprocess
import threading

import numpy as np
import pytest

import audio
from conftest import FFMPEG, decode, dominant_freq, needs_ffmpeg

pytestmark = needs_ffmpeg

ENGINES = ["fallback"] + (["rubberband"] if audio.has_rubberband() else [])


def test_rubberband_detected_in_this_environment():
    # このサンドボックスの ffmpeg には rubberband がある前提(無い環境ではスキップ)
    if not audio.has_rubberband():
        pytest.skip("ffmpeg without rubberband")
    assert "rubberband" in ENGINES


def test_disable_rubberband_env(monkeypatch):
    monkeypatch.setenv("KEYCHANGER_NO_RUBBERBAND", "1")
    assert audio.has_rubberband() is False
    assert audio.choose_engine() == "fallback"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("semitones", [12, -12, 7, -5, 2, 0])
def test_pitch_shift_keeps_tempo(tmp_path, tone_dir, engine, semitones):
    src = tone_dir / "tone440.wav"
    dst = tmp_path / f"out_{engine}_{semitones}.mp3"
    used = audio.render(src, dst, semitones, engine=engine)
    assert used == engine
    orig = decode(src)
    out = decode(dst)
    expected = 440 * 2 ** (semitones / 12)
    got = dominant_freq(out)
    assert got == pytest.approx(expected, rel=0.02), (engine, semitones, got, expected)
    assert len(out) / 44100 == pytest.approx(len(orig) / 44100, rel=0.02)


def test_build_filter_shapes():
    assert audio.build_filter(0, "rubberband").startswith("aresample=44100")
    assert "rubberband" not in audio.build_filter(0, "rubberband")
    assert "rubberband=pitch=2.0000000000" in audio.build_filter(12, "rubberband")
    f = audio.build_filter(-12, "fallback")
    assert "asetrate=22050.0000" in f and "atempo=2.0" in f and "rubberband" not in f


def test_rubberband_uses_quality_options():
    # 声の響きを保つ・左右をまとめて処理する・高品質な計算(音質のための設定)
    f = audio.build_filter(5, "rubberband")
    for opt in ("tempo=1", "pitchq=quality", "formant=preserved", "channels=together"):
        assert opt in f
    assert audio.rubberband_filter(5) in f


def test_resample_uses_soxr_when_available(monkeypatch):
    monkeypatch.setattr(audio, "has_soxr", lambda ffmpeg=None: True)
    assert audio.resample_filter() == "aresample=44100:resampler=soxr:precision=28"
    assert audio.build_filter(-12, "fallback").count("resampler=soxr") == 2
    monkeypatch.setattr(audio, "has_soxr", lambda ffmpeg=None: False)
    assert audio.resample_filter() == "aresample=44100"
    assert "soxr" not in audio.build_filter(3, "rubberband")


def test_disable_soxr_env(monkeypatch):
    monkeypatch.setenv("KEYCHANGER_NO_SOXR", "1")
    assert audio.has_soxr() is False


def test_mp3_is_320k():
    assert audio._encode_args()[audio._encode_args().index("-b:a") + 1] == "320k"


@pytest.mark.parametrize("bad", [13, -13, 100, 1.5, "3", True])
def test_validate_semitones_rejects(bad):
    with pytest.raises(ValueError):
        audio.validate_semitones(bad)


def test_atempo_chain_splits():
    assert audio._atempo_chain(4.0) == ["atempo=2.0", "atempo=2.0000000000"]
    assert audio._atempo_chain(0.25)[0] == "atempo=0.5"


def test_probe(tone_dir):
    p = audio.probe(tone_dir / "tone440.wav")
    assert p["has_audio"] and p["duration"] == pytest.approx(5.0, abs=0.05)
    assert audio.probe(tone_dir / "notaudio.mp3")["has_audio"] is False


def test_probe_japanese_metadata_under_cp932_locale(tmp_path, monkeypatch):
    """日本語 Windows(ロケール cp932)でも ffmpeg の UTF-8 出力で落ちないこと。"""
    import subprocess

    from conftest import FFMPEG
    src = tmp_path / "日本語フォルダ" / "曲.mp3"
    src.parent.mkdir()
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", "-metadata", "title=夜に駆ける",
                    "-metadata", "artist=YOASOBI", str(src)], check=True)
    real_run = subprocess.run

    def run(*a, **kw):
        # text=True だけで呼ぶと cp932 で厳密デコードされる環境を再現
        if kw.get("text") and not kw.get("encoding"):
            kw["encoding"] = "cp932"
        return real_run(*a, **kw)
    monkeypatch.setattr(audio.subprocess, "run", run)
    p = audio.probe(src)
    assert p["has_audio"] and p["duration"] == pytest.approx(2.0, abs=0.1)
    out = tmp_path / "out.mp3"
    assert audio.render(src, out, 2, engine="fallback") == "fallback"


# ---------------------------------------------------------------- 並列変換
needs_rubberband = pytest.mark.skipif(not audio.has_rubberband(), reason="ffmpeg without rubberband")


def test_probe_channels(tmp_path, tone_dir):
    assert audio.probe(tone_dir / "tone440.wav")["channels"] == 2
    mono = tmp_path / "mono.wav"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i",
                    str(tone_dir / "tone440.wav"), "-ac", "1", str(mono)], check=True)
    assert audio.probe(mono)["channels"] == 1


def test_segment_frames():
    r = audio.WORK_RATE
    assert audio.segment_frames(240, 1) is None            # 1 プロセスなら並列にしない
    assert audio.segment_frames(15, 8) is None             # 短い音声は並列にしない
    assert audio.segment_frames(240, 4) == 15 * r          # 4 分 ÷ (4 × 4 区間)
    assert audio.segment_frames(240, 16) == 10 * r         # 区間は 10 秒より短くしない
    assert audio.segment_frames(None, 4) == 10 * r         # 長さが分からなくても並列にする
    assert audio.segment_frames(1200, 2) % (r // 100) == 0  # 10 ミリ秒単位


def _noise(n, seed):
    rnd = random.Random(seed)
    return [rnd.uniform(-1, 1) for _ in range(n)]


@pytest.mark.parametrize("shift", [0, 5, -37, 100])
def test_find_seam_finds_time_shift(shift):
    ml = audio.MAX_LAG
    x = _noise(audio.OVERLAP + 4 * ml, 1)
    a = x[2 * ml: 2 * ml + audio.OVERLAP]
    # b は a と同じ波形が shift サンプルだけ遅れたもの(b[ml + i + shift] == a[i])
    b = x[ml - shift: ml - shift + audio.OVERLAP + 2 * ml]
    p, lag, rho = audio.find_seam(a, b)
    assert lag == shift
    assert rho == pytest.approx(1.0, abs=1e-6)
    assert a[p:p + audio.XFADE] == b[ml + p + lag: ml + p + lag + audio.XFADE]


def test_find_seam_picks_the_place_where_waveforms_agree():
    ml = audio.MAX_LAG
    a = _noise(audio.OVERLAP, 2)
    b = _noise(audio.OVERLAP + 2 * ml, 3)       # ほとんどの場所では a と無関係
    lo, hi = 22050, 22050 + 4000                # ここだけ a と一致している
    b[ml + lo: ml + hi] = a[lo:hi]
    p, lag, rho = audio.find_seam(a, b)
    assert lag == 0 and lo <= p <= hi - audio.XFADE
    assert rho == pytest.approx(1.0, abs=1e-6)


def test_find_seam_silence():
    p, lag, rho = audio.find_seam([0.0] * audio.OVERLAP, [0.0] * (audio.OVERLAP + 2 * audio.MAX_LAG))
    assert rho == 1.0 and 0 <= p <= audio.OVERLAP - audio.XFADE


def test_crossfade():
    n, ch = audio.XFADE, 2
    ones = array.array("f", [1.0] * (n * ch)).tobytes()
    zeros = bytes(len(ones))
    same = np.frombuffer(audio.crossfade(ones, ones, ch, 1.0), dtype="<f4")
    assert np.allclose(same, 1.0, atol=1e-6)            # 同じ波形どうしなら何も変わらない
    fade = np.frombuffer(audio.crossfade(ones, zeros, ch, 1.0), dtype="<f4").reshape(n, ch)
    assert np.all(np.diff(fade[:, 0]) < 0) and fade[0, 0] > 0.99 and fade[-1, 0] < 0.01
    assert np.array_equal(fade[:, 0], fade[:, 1])
    # 一致していない波形どうし(rho=0)では、真ん中で音量が下がらないよう持ち上げる
    mid = np.frombuffer(audio.crossfade(ones, zeros, ch, 0.0), dtype="<f4").reshape(n, ch)[n // 2, 0]
    assert mid == pytest.approx(2 ** -0.5, abs=0.01)


def _frame_db(x, size=441):
    n = len(x) // size
    return 10 * np.log10((x[:n * size].reshape(n, size) ** 2).mean(axis=1) + 1e-12)


@needs_rubberband
@pytest.mark.parametrize("semitones", [3, -4])
def test_parallel_render_matches_single(tmp_path, tone_dir, monkeypatch, semitones):
    """区切って並列に変換しても、1 本で変換したときと同じ長さ・音程・音量になること。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)   # 14 秒の音源でも 5 区間に分かれるようにする
    seams = []
    real = audio.find_seam
    monkeypatch.setattr(audio, "find_seam", lambda a, b: seams.append(real(a, b)) or seams[-1])
    src = tone_dir / "song.wav"
    par, one = tmp_path / "par.mp3", tmp_path / "one.mp3"
    assert audio.render(src, par, semitones, engine="rubberband", jobs=4) == "rubberband"
    assert len(seams) == 4                                # 並列の経路を通った(つなぎ目が 4 つ)
    audio.render(src, one, semitones, engine="rubberband", jobs=1)
    assert len(seams) == 4                                # jobs=1 は従来どおり 1 本で変換
    a, b = decode(par), decode(one)
    assert abs(len(a) - len(b)) < 0.03 * 44100
    assert dominant_freq(a) == pytest.approx(330 * 2 ** (semitones / 12), rel=0.01)
    assert dominant_freq(a) == pytest.approx(dominant_freq(b), rel=0.005)
    da, db = _frame_db(a)[20:-20], _frame_db(b)[20:-20]   # 10 ミリ秒ごとの音量
    assert da.mean() == pytest.approx(db.mean(), abs=0.5)
    assert da.min() > db.min() - 3                        # つなぎ目で音が途切れていない
    assert da.max() < db.max() + 3
    assert all(rho > 0.8 for _, _, rho in seams), seams   # つなぎ目では波形がよく一致している
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.mp3", "par.mp3"]  # 作業ファイルが残らない


@needs_rubberband
def test_parallel_render_keeps_mono_and_tags(tmp_path, tone_dir, monkeypatch):
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    src = tmp_path / "mono.mp3"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(tone_dir / "song.wav"),
                    "-ac", "1", "-metadata", "title=夜に駆ける", str(src)], check=True)
    out = tmp_path / "out.mp3"
    audio.render(src, out, 2, engine="rubberband", jobs=4)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", str(out)], capture_output=True,
                          encoding="utf-8", errors="replace").stderr
    assert "夜に駆ける" in info
    assert audio.probe(out)["channels"] == 1
    assert audio.probe(out)["duration"] == pytest.approx(14.0, abs=0.1)


@needs_rubberband
def test_parallel_render_falls_back_to_single(tmp_path, tone_dir, monkeypatch):
    """並列変換の途中で失敗しても、1 本での変換に切り替えて最後まで変換できること。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)

    def boom(a, b):
        raise audio.AudioError("boom")
    monkeypatch.setattr(audio, "find_seam", boom)
    out = tmp_path / "out.mp3"
    audio.render(tone_dir / "song.wav", out, 2, engine="rubberband", jobs=4)
    assert dominant_freq(decode(out)) == pytest.approx(330 * 2 ** (2 / 12), rel=0.01)
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp3"]


@needs_rubberband
@pytest.mark.parametrize("jobs", [1, 4])
def test_render_can_be_cancelled(tmp_path, tone_dir, monkeypatch, jobs):
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    cancel = threading.Event()
    real = audio.subprocess.Popen
    started = []

    def popen(cmd, *a, **kw):
        proc = real(cmd, *a, **kw)
        started.append(proc)
        if any("rubberband" in str(c) for c in cmd):
            cancel.set()   # キー変換が始まったところで取り消す
        return proc
    monkeypatch.setattr(audio.subprocess, "Popen", popen)
    out = tmp_path / "out.mp3"
    with pytest.raises(audio.RenderCancelled):
        audio.render(tone_dir / "song.wav", out, 5, engine="rubberband", jobs=jobs, cancel=cancel)
    assert list(tmp_path.iterdir()) == []                 # 途中のファイルを残さない
    assert started and all(p.poll() is not None for p in started)  # ffmpeg を残さない


def test_render_already_cancelled(tmp_path, tone_dir):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(audio.RenderCancelled):
        audio.render(tone_dir / "tone440.wav", tmp_path / "out.mp3", 2, engine="fallback", cancel=cancel)
    assert list(tmp_path.iterdir()) == []
