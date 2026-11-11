import pytest

import audio
from conftest import decode, dominant_freq, needs_ffmpeg

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
