import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
import audio
from conftest import decode, dominant_freq, needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(appmod.settings, "max_duration", 20 * 60)
    monkeypatch.setattr(appmod.settings, "engine", None)
    with TestClient(appmod.app, base_url="http://127.0.0.1:8000") as c:
        yield c


def wait_job(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("job timeout")


def load(client, url):
    r = client.post("/api/load", json={"url": url})
    assert r.status_code == 200, r.text
    return wait_job(client, r.json()["job"])


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "キー変更アプリ" in r.text
    assert client.get("/app.js").status_code == 200


def test_status(client):
    s = client.get("/api/status").json()
    assert s["ffmpeg"] is True
    assert s["engine"] in ("rubberband", "fallback")
    assert s["max_minutes"] == 20


@pytest.mark.parametrize("url", ["", "abc", "ftp://example.com/a.mp3", "file:///etc/passwd",
                                 "javascript:alert(1)", "http://"])
def test_load_rejects_bad_urls(client, url):
    r = client.post("/api/load", json={"url": url})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_load_direct_url_render_and_download(client, http_server, tmp_path):
    j = load(client, f"{http_server}/tone440.mp3")
    assert j["status"] == "done", j
    meta = j["result"]
    assert meta["title"] == "tone440"
    assert meta["duration"] == pytest.approx(5.0, abs=0.2)
    sid = meta["id"]

    # 同じ URL はキャッシュから即返る
    j2 = load(client, f"{http_server}/tone440.mp3")
    assert j2["result"]["id"] == sid

    r = client.post("/api/render", json={"id": sid, "semitones": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == f"/api/audio/{sid}?semitones=3"
    assert body["filename"] == "tone440 (+3).mp3"

    a = client.get(body["download_url"])
    assert a.status_code == 200
    assert a.headers["content-type"] == "audio/mpeg"
    cd = a.headers["content-disposition"]
    assert cd.startswith("attachment") and "tone440%20%28%2B3%29.mp3" in cd
    out = tmp_path / "dl.mp3"
    out.write_bytes(a.content)
    assert dominant_freq(decode(out)) == pytest.approx(440 * 2 ** (3 / 12), rel=0.02)

    inline = client.get(body["url"])
    assert inline.headers["content-disposition"].startswith("inline")
    # Range リクエスト(シーク用)
    part = client.get(body["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100

    zero = client.get(f"/api/audio/{sid}?semitones=0&download=1")
    assert zero.status_code == 200
    assert "%C2%B10" in zero.headers["content-disposition"]  # (±0)


def test_load_missing_file_reports_error(client, http_server):
    j = load(client, f"{http_server}/nothing-here.mp3")
    assert j["status"] == "error"
    assert "失敗" in j["error"] or "対応" in j["error"] or "利用できません" in j["error"]


def test_load_non_audio_url(client, http_server):
    j = load(client, f"{http_server}/notaudio.mp3")
    assert j["status"] == "error"


def test_load_too_long(client, http_server, monkeypatch):
    monkeypatch.setattr(appmod.settings, "max_duration", 2)
    j = load(client, f"{http_server}/tone440.wav")
    assert j["status"] == "error"
    assert "長すぎます" in j["error"]


def test_load_too_big(client, http_server, monkeypatch):
    monkeypatch.setattr(appmod.settings, "max_filesize", 1000)
    j = load(client, f"{http_server}/tone440.wav")
    assert j["status"] == "error"
    assert "大きすぎ" in j["error"]


def test_playlist_rejected(client, monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            assert opts["noplaylist"] is True
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            return {"_type": "playlist", "entries": [{"id": "a"}], "title": "pl"}
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    j = load(client, "https://www.youtube.com/playlist?list=PL123")
    assert j["status"] == "error" and "プレイリスト" in j["error"]


def test_youtube_error_translated(client, monkeypatch):
    import yt_dlp

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Sign in to confirm you're not a bot")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    j = load(client, "https://www.youtube.com/watch?v=abc")
    assert j["status"] == "error" and "ログイン" in j["error"]


def test_ffmpeg_missing(client, monkeypatch):
    monkeypatch.setattr(audio, "find_ffmpeg", lambda: None)
    r = client.post("/api/load", json={"url": "https://example.com/a.mp3"})
    assert r.status_code == 500 and "ffmpeg" in r.json()["detail"]
    assert client.get("/api/status").json()["ffmpeg"] is False


def test_upload_and_render(client, tone_dir):
    with open(tone_dir / "tone440.wav", "rb") as f:
        r = client.post("/api/upload", files={"file": ("私の曲.wav", f, "audio/wav")})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["title"] == "私の曲"
    r = client.post("/api/render", json={"id": meta["id"], "semitones": -2})
    assert r.status_code == 200
    assert r.json()["filename"] == "私の曲 (-2).mp3"
    assert client.get(r.json()["url"]).status_code == 200


def test_upload_rejects_non_audio(client, tone_dir):
    r = client.post("/api/upload", files={"file": ("x.mp3", b"not audio" * 50, "audio/mpeg")})
    assert r.status_code == 400
    r = client.post("/api/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
    assert "形式" in r.json()["detail"]


def test_upload_too_big(client, monkeypatch, tone_dir):
    monkeypatch.setattr(appmod.settings, "max_filesize", 1000)
    with open(tone_dir / "tone440.wav", "rb") as f:
        r = client.post("/api/upload", files={"file": ("t.wav", f, "audio/wav")})
    assert r.status_code == 400 and "大きすぎ" in r.json()["detail"]


@pytest.mark.parametrize("n", [13, -13, 99])
def test_render_semitone_range(client, tone_dir, n):
    with open(tone_dir / "tone440.wav", "rb") as f:
        sid = client.post("/api/upload", files={"file": ("t.wav", f, "audio/wav")}).json()["id"]
    r = client.post("/api/render", json={"id": sid, "semitones": n})
    assert r.status_code == 400
    assert client.get(f"/api/audio/{sid}?semitones={n}").status_code == 422


def test_render_semitone_type(client):
    r = client.post("/api/render", json={"id": "a" * 20, "semitones": "abc"})
    assert r.status_code == 422


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "../../etc/passwd", "ABCDEF0123456789ABCD", "zz", "a" * 21])
def test_bad_ids(client, bad):
    r = client.post("/api/render", json={"id": bad, "semitones": 0})
    assert r.status_code == 400
    r = client.get(f"/api/audio/{bad}?semitones=0")
    assert r.status_code in (400, 404)


def test_unknown_id(client):
    r = client.post("/api/render", json={"id": "0" * 20, "semitones": 1})
    assert r.status_code == 404


def test_unknown_job(client):
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/jobs/../../x").status_code == 404


def test_fallback_engine_via_api(client, tone_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(appmod.settings, "engine", "fallback")
    with open(tone_dir / "tone440.wav", "rb") as f:
        sid = client.post("/api/upload", files={"file": ("t.wav", f, "audio/wav")}).json()["id"]
    a = client.get(f"/api/audio/{sid}?semitones=12")
    out = tmp_path / "o.mp3"
    out.write_bytes(a.content)
    assert dominant_freq(decode(out)) == pytest.approx(880, rel=0.02)


def test_cleanup_cache_caps_size(client, monkeypatch, tone_dir):
    with open(tone_dir / "tone440.wav", "rb") as f:
        sid = client.post("/api/upload", files={"file": ("t.wav", f, "audio/wav")}).json()["id"]
    client.post("/api/render", json={"id": sid, "semitones": 1})
    monkeypatch.setattr(appmod.settings, "max_cache_bytes", 0)
    appmod.cleanup_cache()
    assert list(appmod.sources_dir().iterdir()) == []
    assert list(appmod.renders_dir().iterdir()) == []


def test_safe_filename():
    assert appmod.safe_filename('a/b:c*?"<>|', 2) == "a_b_c______ (+2).mp3"
    assert appmod.safe_filename("", -1) == "audio (-1).mp3"
    assert appmod.safe_filename("x" * 200, 0).endswith("(±0).mp3")


# ---------------------------------------------------------------- レビュー指摘の回帰テスト
@pytest.fixture()
def endless_server(tone_dir):
    """Content-Length なしで mp3 を流し続ける(ネットラジオ相当)サーバー。送信量を記録する。"""
    import http.server
    import threading

    chunk = (tone_dir / "tone440.mp3").read_bytes()
    sent = {"bytes": 0}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a): pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()

        def do_GET(self):
            self.do_HEAD()
            try:
                while sent["bytes"] < 30 * 1024 * 1024:  # 安全のため 30MB で打ち切り
                    self.wfile.write(chunk)
                    sent["bytes"] += len(chunk)
            except OSError:
                pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/radio.mp3", sent
    srv.shutdown()


def test_stream_without_content_length_is_cut_off(client, endless_server, monkeypatch):
    url, sent = endless_server
    monkeypatch.setattr(appmod.settings, "max_filesize", 1024 * 1024)
    j = load(client, url)
    assert j["status"] == "error" and "大きすぎ" in j["error"], j
    assert sent["bytes"] < 10 * 1024 * 1024, sent  # 上限付近で止まっている
    assert not [p for p in appmod.sources_dir().iterdir() if p.name.startswith(".tmp-")]


def test_download_wall_clock_limit(client, endless_server, monkeypatch):
    url, _ = endless_server
    monkeypatch.setattr(appmod.settings, "max_download_seconds", 0)
    j = load(client, url)
    assert j["status"] == "error" and "時間がかかりすぎ" in j["error"], j


def test_cleanup_keeps_in_progress_part_files(client, monkeypatch):
    import os
    r = appmod.renders_dir()
    fresh = r / ("a" * 20 + "_+3.mp3.part")
    old = r / ("b" * 20 + "_+3.mp3.part")
    fresh.write_bytes(b"x")
    old.write_bytes(b"x")
    t = time.time() - 2 * 3600
    os.utime(old, (t, t))
    appmod.cleanup_cache()
    assert fresh.exists() and not old.exists()


def test_cleanup_continues_when_a_file_is_locked(client, monkeypatch, tone_dir):
    import pathlib
    with open(tone_dir / "tone440.wav", "rb") as f:
        sid = client.post("/api/upload", files={"file": ("t.wav", f, "audio/wav")}).json()["id"]
    client.post("/api/render", json={"id": sid, "semitones": 1})
    client.post("/api/render", json={"id": sid, "semitones": 2})
    locked = appmod.render_path(sid, 1)
    real_unlink = pathlib.Path.unlink

    def unlink(self, missing_ok=False):
        if self == locked:
            raise PermissionError("in use")  # Windows で使用中のファイル
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    monkeypatch.setattr(appmod.settings, "max_cache_bytes", 0)
    appmod.cleanup_cache()  # 例外にならない
    assert locked.exists()
    assert not appmod.render_path(sid, 2).exists()
    assert not (appmod.sources_dir() / sid).exists()


@pytest.mark.parametrize("msg,warn", [
    ("ERROR: [youtube] abc: Requested format is not available. Use --list-formats for a list of available formats", []),
    ("ERROR: [youtube] abc: This video is unavailable", ["[youtube] No supported JavaScript runtime could be found"]),
])
def test_js_runtime_errors_point_to_deno(msg, warn):
    out = appmod._translate_ytdlp_error(msg, warn)
    assert "deno" in out and "yt-dlp" in out


def test_unavailable_without_js_hint():
    out = appmod._translate_ytdlp_error("ERROR: [youtube] abc: Video unavailable", [])
    assert "利用できません" in out


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "192.168.0.10:8000"])
def test_rejects_foreign_host_header(client, host):
    r = client.get("/api/status", headers={"Host": host})
    assert r.status_code == 400
    r = client.post("/api/load", json={"url": "https://example.com/a.mp3"}, headers={"Host": host})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"])
def test_accepts_local_host_header(client, host):
    assert client.get("/api/status", headers={"Host": host}).status_code == 200
