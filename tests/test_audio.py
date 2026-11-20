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
    dst = tmp_path / f"out_{engine}_{semitones}.wav"
    used = audio.render(src, dst, semitones, engine=engine)
    assert used == engine
    assert audio.probe(dst)["codec"] == "pcm_f32le"       # 再生用は WAV(MP3 にはしない)
    orig = decode(src)
    out = decode(dst)
    expected = 440 * 2 ** (semitones / 12)
    got = dominant_freq(out)
    assert got == pytest.approx(expected, rel=0.02), (engine, semitones, got, expected)
    assert len(out) / 44100 == pytest.approx(len(orig) / 44100, rel=0.02)


def test_build_filter_shapes():
    assert audio.build_filter(0, "rubberband") == "aresample=44100"
    assert "rubberband=pitch=2.0000000000" in audio.build_filter(12, "rubberband")
    f = audio.build_filter(-12, "fallback")
    assert "asetrate=22050.0000" in f and "atempo=2.0" in f and "rubberband" not in f


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


def _convert(src, dst, *args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *args, str(dst)],
                   check=True)
    return dst


def test_probe_reports_container_and_codec(tmp_path, tone_dir):
    """ブラウザでそのまま再生できるかを決めるための情報が取れること。"""
    wav = audio.probe(tone_dir / "tone440.wav")
    assert (wav["format"], wav["codec"], wav["audio_streams"], wav["video"]) == ("wav", "pcm_s16le", 1, False)
    m4a = audio.probe(_convert(tone_dir / "tone440.wav", tmp_path / "a.m4a", "-c:a", "aac"))
    assert "m4a" in m4a["format"].split(",") and m4a["codec"] == "aac" and m4a["profile"] == "LC"
    assert m4a["layout"] == "stereo" and m4a["start"] == pytest.approx(0.0, abs=0.05)
    webm = audio.probe(_convert(tone_dir / "tone440.wav", tmp_path / "a.webm", "-c:a", "libopus"))
    assert "webm" in webm["format"].split(",") and webm["codec"] == "opus" and webm["profile"] is None
    mp3 = audio.probe(tone_dir / "tone440.mp3")
    assert mp3["format"] == "mp3" and mp3["codec"] == "mp3"
    nothing = audio.probe(tone_dir / "notaudio.mp3")
    assert nothing["codec"] is None and nothing["audio_streams"] == 0


def test_probe_tells_video_from_cover_art(tmp_path, tone_dir):
    video = tmp_path / "video.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1:r=5", "-i", str(tone_dir / "tone440.wav"),
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video)], check=True)
    assert audio.probe(video)["video"] is True
    cover = tmp_path / "cover.m4a"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(tone_dir / "tone440.wav"), "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                    "-map", "0:a", "-map", "1:v", "-frames:v", "1", "-c:a", "aac", "-c:v", "mjpeg",
                    "-disposition:v", "attached_pic", str(cover)], check=True)
    info = audio.probe(cover)
    assert info["video"] is False and info["codec"] == "aac"    # ジャケット画像は映像に数えない


def test_segment_frames():
    r = audio.WORK_RATE
    assert audio.segment_frames(240, 1) is None            # 1 プロセスなら並列にしない
    assert audio.segment_frames(15, 8) is None             # 短い音声は並列にしない
    assert audio.segment_frames(240, 4) == 60 * r          # 4 分 ÷ 4 区間(プロセスごとに 1 区間)
    assert audio.segment_frames(240, 16) == 15 * r
    assert audio.segment_frames(100, 16) == 10 * r         # 区間は 10 秒より短くしない
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
    n, ch, full = audio.XFADE, 2, 0.5
    loud = array.array("f", [full] * (n * ch)).tobytes()
    zeros = bytes(len(loud))
    same = np.frombuffer(audio.crossfade(loud, loud, ch, 1.0), dtype="<f4")
    assert np.allclose(same, full, atol=1e-6)           # 同じ波形どうしなら何も変わらない
    fade = np.frombuffer(audio.crossfade(loud, zeros, ch, 1.0), dtype="<f4").reshape(n, ch) / full
    assert np.all(np.diff(fade[:, 0]) < 0) and fade[0, 0] > 0.99 and fade[-1, 0] < 0.01
    assert np.array_equal(fade[:, 0], fade[:, 1])
    # 一致していない波形どうし(rho=0)では、真ん中で音量が下がらないよう持ち上げる
    mid = np.frombuffer(audio.crossfade(loud, zeros, ch, 0.0), dtype="<f4").reshape(n, ch)[n // 2, 0]
    assert mid / full == pytest.approx(2 ** -0.5, abs=0.01)


def test_crossfade_keeps_values_over_full_scale():
    """フルスケール(±1.0)を超える値も、切り詰めずにそのまま通すこと(切り詰めると音が歪む)。"""
    n = audio.XFADE
    over = array.array("f", [1.5, -1.5] * (n // 2)).tobytes()
    out = np.frombuffer(audio.crossfade(over, over, 1, 1.0), dtype="<f4")
    assert np.allclose(np.abs(out), 1.5, atol=1e-6)


def test_wav_header():
    h = audio._wav_header(2, 1000)
    assert len(h) == audio.WAV_HEADER_BYTES == 58
    assert h[:4] == b"RIFF" and h[8:16] == b"WAVEfmt " and int.from_bytes(h[4:8], "little") == 58 - 8 + 1000
    assert int.from_bytes(h[16:20], "little") == 18           # fmt チャンクの大きさ
    assert int.from_bytes(h[20:22], "little") == 3            # 形式 = IEEE float
    assert int.from_bytes(h[22:24], "little") == 2            # チャンネル数
    assert int.from_bytes(h[24:28], "little") == 44100        # サンプルレート
    assert int.from_bytes(h[28:32], "little") == 44100 * 8    # 1 秒あたりのバイト数
    assert int.from_bytes(h[32:34], "little") == 8            # 1 フレームのバイト数
    assert int.from_bytes(h[34:36], "little") == 32           # 32bit
    assert h[38:42] == b"fact" and int.from_bytes(h[46:50], "little") == 1000 // 8   # フレーム数
    assert h[50:54] == b"data" and int.from_bytes(h[54:58], "little") == 1000


def _loud_source(path):
    """フルスケールぎりぎりまで詰めた、音の大きい音源(14 秒)。キーを変えると山がフルスケールを超える。"""
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "aevalsrc=0.99*sgn(sin(2*PI*220*t))|0.99*sgn(sin(2*PI*330*t)):d=14:s=44100",
         "-c:a", "pcm_s16le", str(path)], check=True)
    return path


@needs_rubberband
@pytest.mark.parametrize("jobs", [1, 4])
def test_render_keeps_peaks_over_full_scale(tmp_path, monkeypatch, jobs):
    """キーを変えるとフルスケールを超える山ができる。再生用の WAV でも保存用の MP3 でも、それを
    切り詰めない(切り詰めると歪みになる。以前の、変換結果をそのまま MP3 にしていたときと同じ音にする)。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    src = _loud_source(tmp_path / "loud.wav")
    assert np.abs(_decode_stereo(src)).max() <= 1.0
    wav, mp3 = tmp_path / "out.wav", tmp_path / "out.mp3"
    audio.render(src, wav, 3, engine="rubberband", jobs=jobs)
    peak = np.abs(_decode_stereo(wav)).max()
    assert peak > 1.05, peak
    audio.encode_mp3(wav, mp3, tags_from=src)
    assert np.abs(_decode_stereo(mp3)).max() > 1.05


def _decode_stereo(path):
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def _frame_db(x, size=441):
    n = len(x) // size
    return 10 * np.log10((x[:n * size].reshape(n, size) ** 2).mean(axis=1) + 1e-12)


@needs_rubberband
@pytest.mark.parametrize("semitones", [3, -4])
def test_parallel_render_matches_single(tmp_path, tone_dir, monkeypatch, semitones):
    """区切って並列に変換しても、1 本で変換したときと同じ長さ・音程・音量になること。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)   # 14 秒の音源でも 4 区間に分かれるようにする
    seams = []
    real = audio.find_seam
    monkeypatch.setattr(audio, "find_seam", lambda a, b: seams.append(real(a, b)) or seams[-1])
    src = tone_dir / "song.wav"
    par, one = tmp_path / "par.wav", tmp_path / "one.wav"
    assert audio.render(src, par, semitones, engine="rubberband", jobs=4) == "rubberband"
    assert len(seams) == 3                                # 並列の経路を通った(つなぎ目が 3 つ)
    audio.render(src, one, semitones, engine="rubberband", jobs=1)
    assert len(seams) == 3                                # jobs=1 は従来どおり 1 本で変換
    a, b = decode(par), decode(one)
    assert abs(len(a) - len(b)) < 0.03 * 44100
    assert dominant_freq(a) == pytest.approx(330 * 2 ** (semitones / 12), rel=0.01)
    assert dominant_freq(a) == pytest.approx(dominant_freq(b), rel=0.005)
    da, db = _frame_db(a)[20:-20], _frame_db(b)[20:-20]   # 10 ミリ秒ごとの音量
    assert da.mean() == pytest.approx(db.mean(), abs=0.5)
    assert da.min() > db.min() - 3                        # つなぎ目で音が途切れていない
    assert da.max() < db.max() + 3
    assert all(rho > 0.8 for _, _, rho in seams), seams   # つなぎ目では波形がよく一致している
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.wav", "par.wav"]  # 作業ファイルが残らない
    # 自前で書いた WAV の先頭(大きさ)が中身と合っている
    size = par.stat().st_size
    head = par.read_bytes()[:58]
    assert head == audio._wav_header(2, size - 58)
    assert audio.probe(par)["duration"] == pytest.approx((size - 58) / 8 / 44100, abs=0.02)


@needs_rubberband
def test_parallel_render_keeps_mono(tmp_path, tone_dir, monkeypatch):
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    src = _convert(tone_dir / "song.wav", tmp_path / "mono.mp3", "-ac", "1")
    out = tmp_path / "out.wav"
    audio.render(src, out, 2, engine="rubberband", jobs=4)
    info = audio.probe(out)
    assert info["channels"] == 1 and info["codec"] == "pcm_f32le"
    assert info["duration"] == pytest.approx(14.0, abs=0.1)
    assert out.read_bytes()[:58] == audio._wav_header(1, out.stat().st_size - 58)


@pytest.mark.parametrize("jobs", [1, 4])
def test_render_mixes_surround_down_to_stereo(tmp_path, tone_dir, monkeypatch, jobs):
    """5.1ch の音源も、変換結果はステレオにする(ブラウザで確実に再生できるように)。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    src = _convert(tone_dir / "song.wav", tmp_path / "surround.wav", "-ac", "6")
    out = tmp_path / "out.wav"
    audio.render(src, out, 2 if audio.has_rubberband() else 0, jobs=jobs)
    assert audio.probe(out)["channels"] == 2 and audio.probe(out)["layout"] in ("stereo", "2 channels")
    assert int.from_bytes(out.read_bytes()[22:24], "little") == 2


def _tags(path):
    return subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                          encoding="utf-8", errors="replace").stderr


def test_encode_mp3_copies_tags_from_the_source(tmp_path, tone_dir):
    """保存用の MP3 は変換結果(WAV)から作り、曲名などのタグは元の音源から写す。"""
    src = _convert(tone_dir / "tone440.wav", tmp_path / "src.m4a", "-c:a", "aac",
                   "-metadata", "title=夜に駆ける")
    wav = tmp_path / "shift.wav"
    audio.render(src, wav, 3, engine="fallback")
    assert "夜に駆ける" not in _tags(wav)
    mp3 = tmp_path / "shift.mp3"
    audio.encode_mp3(wav, mp3, tags_from=src)
    info = audio.probe(mp3)
    assert info["codec"] == "mp3" and info["duration"] == pytest.approx(5.0, abs=0.1)
    assert "夜に駆ける" in _tags(mp3)
    assert dominant_freq(decode(mp3)) == pytest.approx(440 * 2 ** (3 / 12), rel=0.02)
    # 原曲キーは音源から直接作る(タグはそのまま引き継がれる)
    same = tmp_path / "same.mp3"
    audio.encode_mp3(src, same)
    assert "夜に駆ける" in _tags(same)
    assert dominant_freq(decode(same)) == pytest.approx(440, rel=0.02)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["same.mp3", "shift.mp3", "shift.wav", "src.m4a"]


def test_encode_mp3_ignores_cover_art(tmp_path, tone_dir):
    src = tmp_path / "cover.m4a"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(tone_dir / "tone440.wav"), "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
                    "-map", "0:a", "-map", "1:v", "-frames:v", "1", "-c:a", "aac", "-c:v", "mjpeg",
                    "-disposition:v", "attached_pic", str(src)], check=True)
    out = tmp_path / "out.mp3"
    audio.encode_mp3(src, out)
    assert audio.probe(out)["codec"] == "mp3" and "Video:" not in _tags(out)


def test_encode_mp3_failure_leaves_nothing(tmp_path, tone_dir):
    with pytest.raises(audio.AudioError):
        audio.encode_mp3(tone_dir / "notaudio.mp3", tmp_path / "out.mp3")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(audio.RenderCancelled):
        audio.encode_mp3(tone_dir / "tone440.wav", tmp_path / "out.mp3", cancel=cancel)
    assert list(tmp_path.iterdir()) == []


@needs_rubberband
def test_parallel_render_falls_back_to_single(tmp_path, tone_dir, monkeypatch):
    """並列変換の途中で失敗しても、1 本での変換に切り替えて最後まで変換できること。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)

    def boom(a, b):
        raise audio.AudioError("boom")
    monkeypatch.setattr(audio, "find_seam", boom)
    out = tmp_path / "out.wav"
    audio.render(tone_dir / "song.wav", out, 2, engine="rubberband", jobs=4)
    assert dominant_freq(decode(out)) == pytest.approx(330 * 2 ** (2 / 12), rel=0.01)
    assert [p.name for p in tmp_path.iterdir()] == ["out.wav"]


def _write_pcm(path, samples):
    path.write_bytes(array.array("f", samples).tobytes())
    return path


def _overlap_files(tmp_path, shift=37):
    """同じ波形が shift サンプルずれて入っている、2 つの「区間の変換結果」(32bit float モノラル)。"""
    rnd = random.Random(5)
    x = [rnd.uniform(-0.6, 0.6) for _ in range(audio.OVERLAP + 6 * audio.MAX_LAG)]
    a = _write_pcm(tmp_path / "a.pcm", x[2 * audio.MAX_LAG:])
    b = _write_pcm(tmp_path / "b.pcm", x[audio.MAX_LAG - shift:])
    return {"a": str(a), "a_off": 0, "b": str(b), "b_off": 0, "channels": 1}


def test_seam_helpers_are_used_only_with_many_jobs():
    assert [audio.seam_helpers(n) for n in (1, 2, 4, 5, 6, 8, 12, 16)] == [0, 0, 0, 0, 2, 2, 4, 4]


def test_seam_helper_process_gives_the_same_answer(tmp_path):
    """つなぐ場所の計算を別プロセスに頼んでも、自分で計算したときと同じ答えになること。"""
    job = _overlap_files(tmp_path)
    expected = audio._seam_job(**job)
    assert expected[1] == 37 and expected[2] == pytest.approx(1.0, abs=1e-6)
    helper = audio._SeamHelper()
    try:
        assert helper.ask(job, lambda: None) == expected
        assert helper.ask(job, lambda: None) == expected      # 続けて何度でも頼める
        # 計算できない頼みごと(ファイルが無い)は、頼んだ側が自分で計算し直せるように知らせる
        with pytest.raises(audio._HelperFailed):
            helper.ask({**job, "a": str(tmp_path / "missing.pcm")}, lambda: None)
        assert helper.ask(job, lambda: None) == expected      # そのあとも使える
    finally:
        helper.close()
    assert helper.proc.poll() is not None                     # プロセスを残さない
    with pytest.raises(audio._HelperFailed):
        helper.ask(job, lambda: None)                         # 終わったものに頼んだら失敗として扱う


def test_seam_helper_keeps_checking_for_cancel(tmp_path, monkeypatch):
    """別プロセスの答えを待っている間も、取り消し・時間切れを見張る。"""
    helper = audio._SeamHelper()
    monkeypatch.setattr(helper.proc.stdin, "write", lambda data: None)   # 頼みごとが届かない = 答えが来ない
    calls = []

    def check():
        calls.append(1)
        if len(calls) >= 3:
            raise audio.RenderCancelled()
    try:
        with pytest.raises(audio.RenderCancelled):
            helper.ask(_overlap_files(tmp_path), check)
    finally:
        helper.close()


@needs_rubberband
def test_parallel_render_with_seam_helpers_is_identical(tmp_path, tone_dir, monkeypatch):
    """つなぐ場所を別プロセスで計算しても、変換結果は 1 バイトも変わらないこと。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    src = tone_dir / "song.wav"
    plain, helped = tmp_path / "plain.wav", tmp_path / "helped.wav"
    audio.render(src, plain, 3, engine="rubberband", jobs=4)  # 4 並列では別プロセスを使わない
    asked, local = [], []
    real_ask, real_find = audio._SeamHelper.ask, audio.find_seam
    monkeypatch.setattr(audio._SeamHelper, "ask",
                        lambda self, job, check: asked.append(job) or real_ask(self, job, check))
    monkeypatch.setattr(audio, "find_seam", lambda a, b: local.append(1) or real_find(a, b))
    monkeypatch.setattr(audio, "seam_helpers", lambda jobs: 2)
    audio.render(src, helped, 3, engine="rubberband", jobs=4)
    assert len(asked) == 3 and local == []                    # つなぎ目 3 つとも別プロセスで計算した
    assert helped.read_bytes() == plain.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["helped.wav", "plain.wav"]


@needs_rubberband
@pytest.mark.parametrize("failure", ["cannot start", "no answer"])
def test_parallel_render_survives_broken_seam_helpers(tmp_path, tone_dir, monkeypatch, failure):
    """別プロセスが使えないときは、自分のプロセスで計算して最後まで変換する。"""
    monkeypatch.setattr(audio, "SEGMENT_MIN_SEC", 3.0)
    monkeypatch.setattr(audio, "seam_helpers", lambda jobs: 2)
    src = tone_dir / "song.wav"
    plain, out = tmp_path / "plain.wav", tmp_path / "out.wav"
    if failure == "cannot start":
        def init(self):
            raise OSError("no python")
        monkeypatch.setattr(audio._SeamHelper, "__init__", init)
    else:
        def ask(self, job, check):
            raise audio._HelperFailed("gone")
        monkeypatch.setattr(audio._SeamHelper, "ask", ask)
    seams = []
    real = audio.find_seam
    monkeypatch.setattr(audio, "find_seam", lambda a, b: seams.append(1) or real(a, b))
    audio.render(src, out, 3, engine="rubberband", jobs=4)
    assert len(seams) == 3
    monkeypatch.setattr(audio, "seam_helpers", lambda jobs: 0)
    audio.render(src, plain, 3, engine="rubberband", jobs=4)
    assert out.read_bytes() == plain.read_bytes()


@needs_rubberband
@pytest.mark.parametrize("jobs", [1, 4, 8])
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
    out = tmp_path / "out.wav"
    with pytest.raises(audio.RenderCancelled):
        audio.render(tone_dir / "song.wav", out, 5, engine="rubberband", jobs=jobs, cancel=cancel)
    assert list(tmp_path.iterdir()) == []                 # 途中のファイルを残さない
    assert started and all(p.poll() is not None for p in started)  # ffmpeg を残さない


def test_render_already_cancelled(tmp_path, tone_dir):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(audio.RenderCancelled):
        audio.render(tone_dir / "tone440.wav", tmp_path / "out.wav", 2, engine="fallback", cancel=cancel)
    assert list(tmp_path.iterdir()) == []
