import functools
import http.server
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def make_tone(path: Path, freq=440, duration=5.0, rate=44100):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={duration}:sample_rate={rate}",
         "-ac", "2", str(path)],
        check=True)
    return path


def decode(path: Path, rate=44100) -> np.ndarray:
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def dominant_freq(samples: np.ndarray, rate=44100) -> float:
    # 端の過渡部分を避けて中央部分を解析
    n = len(samples)
    seg = samples[n // 4: 3 * n // 4]
    seg = seg * np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(len(seg), 1 / rate)
    spec[freqs < 30] = 0
    return float(freqs[int(np.argmax(spec))])


@pytest.fixture(scope="session")
def tone_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("tones")
    make_tone(d / "tone440.wav")
    make_tone(d / "tone440.mp3")
    (d / "notaudio.mp3").write_bytes(b"this is not audio" * 100)
    return d


@pytest.fixture(scope="session")
def http_server(tone_dir):
    handler = functools.partial(_QuietHandler, directory=str(tone_dir))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass
