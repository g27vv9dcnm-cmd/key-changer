import json
import shutil
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
import audio
from conftest import FFMPEG, decode, dominant_freq, needs_ffmpeg

pytestmark = needs_ffmpeg

REAL_WARM = appmod._Extractor.warm  # client が差し替える前の、本物の warm


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(appmod.settings, "max_duration", 20 * 60)
    monkeypatch.setattr(appmod.settings, "engine", None)
    monkeypatch.setattr(appmod.settings, "prefetch", 0)  # 先回り変換は専用のテストでだけ動かす
    monkeypatch.setattr(appmod, "_last_key", {})
    # 使い回す yt-dlp はテストごとに作り直し、起動時に前もって用意するのは専用のテストでだけ行う
    monkeypatch.setattr(appmod, "_extractor", appmod._Extractor())
    monkeypatch.setattr(appmod._Extractor, "warm", lambda self: None)
    with TestClient(appmod.app, base_url="http://127.0.0.1:8000") as c:
        yield c
    assert appmod.mp3_maker.wait_idle(10)


def wait_job(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}?wait=5").json()
        if j["status"] != "running":
            return j
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
    assert body["ready"] is True and body["direct"] is False
    assert body["url"] == f"/api/audio/{sid}?semitones=3"
    assert body["filename"] == "tone440 (+3).mp3"

    # 再生用は WAV(MP3 へのエンコードを待たない)
    inline = client.get(body["url"])
    assert inline.status_code == 200 and inline.headers["content-type"] == "audio/wav"
    cd = inline.headers["content-disposition"]
    assert cd.startswith("inline") and "tone440%20%28%2B3%29.wav" in cd
    played = tmp_path / "play.wav"
    played.write_bytes(inline.content)
    assert dominant_freq(decode(played)) == pytest.approx(440 * 2 ** (3 / 12), rel=0.02)
    # Range リクエスト(シーク用)
    part = client.get(body["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert body["mp3"] is False and not appmod.mp3_path(sid, 3).exists()

    # 保存用の MP3 は、ダウンロードするときに作る
    a = client.get(body["download_url"])
    assert a.status_code == 200
    assert a.headers["content-type"] == "audio/mpeg"
    cd = a.headers["content-disposition"]
    assert cd.startswith("attachment") and "tone440%20%28%2B3%29.mp3" in cd
    out = tmp_path / "dl.mp3"
    out.write_bytes(a.content)
    assert audio.probe(out)["codec"] == "mp3"
    assert dominant_freq(decode(out)) == pytest.approx(440 * 2 ** (3 / 12), rel=0.02)
    assert client.post("/api/render", json={"id": sid, "semitones": 3}).json()["mp3"] is True

    zero = client.get(f"/api/audio/{sid}?semitones=0&download=1")
    assert zero.status_code == 200 and zero.headers["content-type"] == "audio/mpeg"
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
    out = tmp_path / "o.wav"
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
    assert appmod.safe_filename("曲", 3, ".wav") == "曲 (+3).wav"


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
    fresh = r / ("a" * 20 + "_+3.wav.part")
    old = r / ("b" * 20 + "_+3.wav.part")
    fresh.write_bytes(b"x")
    old.write_bytes(b"x")
    t = time.time() - 2 * 3600
    os.utime(old, (t, t))
    # 並列変換の作業フォルダ(名前が .part で終わる)も同じ扱い
    fresh_dir = r / ("c" * 20 + "_+3.wav.abc.part")
    old_dir = r / ("d" * 20 + "_+3.wav.abc.part")
    for d in (fresh_dir, old_dir):
        d.mkdir()
        (d / "seg0.pcm").write_bytes(b"x")
    os.utime(old_dir, (t, t))
    appmod.cleanup_cache()
    assert fresh.exists() and not old.exists()
    assert fresh_dir.exists() and not old_dir.exists()
    # 起動時は、前回の途中で残った作業フォルダを新しくても消す(変換中のファイルは残す)
    appmod.cleanup_cache(startup=True)
    assert fresh.exists() and not fresh_dir.exists()


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


# ---------------------------------------------------------------- 原曲キーは音源をそのまま再生する
def convert(src, dst, *args):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *args, str(dst)],
                   check=True)
    return dst


@pytest.fixture(scope="session")
def media(tone_dir, tmp_path_factory):
    """いろいろな形式の音源。"""
    d = tmp_path_factory.mktemp("media")
    wav = tone_dir / "tone440.wav"
    shutil.copyfile(wav, d / "tone440.wav")
    shutil.copyfile(tone_dir / "tone440.mp3", d / "tone440.mp3")
    convert(wav, d / "aac.m4a", "-c:a", "aac")
    convert(wav, d / "opus.webm", "-c:a", "libopus")
    convert(wav, d / "vorbis.ogg", "-c:a", "libvorbis")
    convert(wav, d / "tone.flac")
    convert(wav, d / "surround.m4a", "-c:a", "aac", "-ac", "6")
    convert(wav, d / "float.wav", "-c:a", "pcm_f32le")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=5:r=5", "-i", str(wav),
                    "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(d / "video.mp4")], check=True)
    return d


def upload(client, path, name=None):
    with open(path, "rb") as f:
        r = client.post("/api/upload", files={"file": (name or path.name, f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def render(client, sid, semitones, **extra):
    r = client.post("/api/render", json={"id": sid, "semitones": semitones, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def rendered(sid=""):
    return sorted(p.name for p in appmod.renders_dir().iterdir() if p.name.startswith(sid))


@pytest.mark.parametrize("name,media_type", [
    ("aac.m4a", "audio/mp4"), ("opus.webm", "audio/webm"), ("vorbis.ogg", "audio/ogg"),
    ("tone.flac", "audio/flac"), ("tone440.wav", "audio/wav")])
def test_original_key_plays_the_source_itself(client, media, name, media_type):
    """ブラウザでそのまま再生できる音源は、読み込んだあとに作り直さない(待ち時間が無い)。"""
    data = (media / name).read_bytes()
    meta = upload(client, media / name)
    assert meta["direct"] == media_type
    sid = meta["id"]
    body = render(client, sid, 0)
    assert body["ready"] is True and body["direct"] is True
    assert body["url"] == f"/api/audio/{sid}?semitones=0"
    a = client.get(body["url"])
    assert a.status_code == 200 and a.headers["content-type"] == media_type
    assert a.headers["content-disposition"].startswith("inline")
    assert a.content == data                                # 音源そのもの
    assert rendered() == []                                 # 何も変換していない
    part = client.get(body["url"], headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == data[10:20]
    # キーを変えたものは、これまでどおり変換する
    assert render(client, sid, 1)["direct"] is False
    assert client.get(f"/api/audio/{sid}?semitones=1").headers["content-type"] == "audio/wav"


@pytest.mark.parametrize("name", ["tone440.mp3", "surround.m4a", "video.mp4", "float.wav"])
def test_other_sources_are_converted_for_the_original_key(client, media, name):
    """そのまま渡すと再生できないかもしれない・聞こえ方が変わる音源は、WAV に直して再生する。"""
    meta = upload(client, media / name)
    assert meta["direct"] is None
    sid = meta["id"]
    body = render(client, sid, 0)
    assert body["direct"] is False and body["url"] == f"/api/audio/{sid}?semitones=0"
    a = client.get(body["url"])
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav"
    assert rendered() == [f"{sid}_+0.wav"]
    info = audio.probe(appmod.render_path(sid, 0))
    assert info["codec"] == "pcm_f32le" and info["channels"] == 2 and not info["video"]
    assert info["duration"] == pytest.approx(5.0, abs=0.2)


def test_compat_plays_converted_audio_instead_of_the_source(client, media):
    """音源をブラウザが再生できなかったとき(画面が compat を付けて頼み直す)は、WAV に直したものを渡す。"""
    sid = upload(client, media / "aac.m4a")["id"]
    body = render(client, sid, 0, compat=True)
    assert body["direct"] is False and body["url"] == f"/api/audio/{sid}?semitones=0&compat=1"
    a = client.get(body["url"])
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav"
    assert rendered() == [f"{sid}_+0.wav"]
    assert render(client, sid, 2, compat=True)["url"] == f"/api/audio/{sid}?semitones=2"


def test_direct_is_worked_out_for_sources_loaded_by_an_older_version(client, media):
    sid = upload(client, media / "opus.webm")["id"]
    p = appmod.sources_dir() / sid / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    del meta["direct"]                                      # 前のバージョンの meta.json には無い
    p.write_text(json.dumps(meta), encoding="utf-8")
    assert render(client, sid, 0)["direct"] is True
    assert json.loads(p.read_text(encoding="utf-8"))["direct"] == "audio/webm"
    assert [f.name for f in p.parent.iterdir() if f.name.endswith(".part")] == []
    # meta.json に書かれた種類が、ファイルの拡張子と合わないときは使わない
    meta["direct"] = "audio/mp4"
    p.write_text(json.dumps(meta), encoding="utf-8")
    assert render(client, sid, 0)["direct"] is False


def test_direct_media_type_rules():
    from pathlib import Path
    ok = {"format": "mov,mp4,m4a,3gp,3g2,mj2", "codec": "aac", "profile": "LC", "layout": "stereo",
          "duration": 5.0, "start": 0.0, "audio_streams": 1, "video": False}
    f = appmod.direct_media_type
    assert f(Path("source.m4a"), ok) == "audio/mp4"
    assert f(Path("source.M4A"), {**ok, "layout": "mono", "start": None}) == "audio/mp4"
    for change in ({"layout": "5.1"}, {"layout": None}, {"video": True}, {"audio_streams": 2},
                   {"start": 1.5}, {"duration": None}, {"codec": "alac"}, {"profile": "xHE-AAC"},
                   {"format": "matroska,webm"}, {"format": None}):
        assert f(Path("source.m4a"), {**ok, **change}) is None, change
    assert f(Path("source.mp3"), {**ok, "format": "mp3", "codec": "mp3"}) is None
    assert f(Path("source.aac"), {**ok, "format": "aac"}) is None


# ---------------------------------------------------------------- 保存用の MP3
def test_mp3_is_made_only_when_saving(client, media, monkeypatch):
    sid = upload(client, media / "aac.m4a")["id"]
    assert render(client, sid, 2)["mp3"] is False
    assert rendered() == [f"{sid}_+2.wav"]
    r = client.post("/api/mp3", json={"id": sid, "semitones": 2})
    assert r.status_code == 200 and r.json() == {"ready": True}
    assert rendered() == [f"{sid}_+2.mp3", f"{sid}_+2.wav"]
    assert audio.probe(appmod.mp3_path(sid, 2))["codec"] == "mp3"
    assert render(client, sid, 2)["mp3"] is True

    def twice(*a, **kw):
        raise AssertionError("MP3 を作り直している")
    monkeypatch.setattr(appmod.audio, "encode_mp3", twice)
    assert client.post("/api/mp3", json={"id": sid, "semitones": 2}).status_code == 200
    a = client.get(f"/api/audio/{sid}?semitones=2&download=1")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg"


def test_mp3_of_the_original_key_is_made_from_the_source(client, media, tmp_path):
    sid = upload(client, media / "aac.m4a")["id"]
    a = client.get(f"/api/audio/{sid}?semitones=0&download=1")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg"
    assert rendered() == [f"{sid}_+0.mp3"]                  # WAV を経由しない
    out = tmp_path / "dl.mp3"
    out.write_bytes(a.content)
    assert dominant_freq(decode(out)) == pytest.approx(440, rel=0.02)


def test_mp3_converts_the_key_first_when_needed(client, media, tmp_path):
    sid = upload(client, media / "aac.m4a")["id"]
    a = client.get(f"/api/audio/{sid}?semitones=-3&download=1")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg"
    assert rendered() == [f"{sid}_-3.mp3", f"{sid}_-3.wav"]
    out = tmp_path / "dl.mp3"
    out.write_bytes(a.content)
    assert dominant_freq(decode(out)) == pytest.approx(440 * 2 ** (-3 / 12), rel=0.02)
    assert appmod._renders == [] and appmod._foreground == 0 and appmod._encodes == set()


def test_mp3_errors(client, media, monkeypatch):
    assert client.post("/api/mp3", json={"id": "0" * 20, "semitones": 1}).status_code == 404
    assert client.post("/api/mp3", json={"id": "zz", "semitones": 1}).status_code == 400
    sid = upload(client, media / "aac.m4a")["id"]
    assert client.post("/api/mp3", json={"id": sid, "semitones": 13}).status_code == 400
    assert client.post("/api/mp3", json={"id": sid, "semitones": "x"}).status_code == 422

    def fail(*a, **kw):
        raise audio.AudioError("boom")
    monkeypatch.setattr(appmod.audio, "encode_mp3", fail)
    r = client.post("/api/mp3", json={"id": sid, "semitones": 0})
    assert r.status_code == 500 and "MP3" in r.json()["detail"]
    assert appmod._encodes == set() and rendered() == []


def test_ensure_mp3_can_refuse_to_start_a_render(client, media):
    """前もって MP3 を作るときは、そのために変換を始めたりしない。"""
    sid = upload(client, media / "aac.m4a")["id"]
    with pytest.raises(audio.RenderCancelled):
        appmod.ensure_mp3(sid, 4, background=True)
    assert rendered() == [] and appmod._renders == []
    appmod.ensure_render(sid, 4)
    assert appmod.ensure_mp3(sid, 4, background=True) == appmod.mp3_path(sid, 4)


@pytest.fixture()
def eager_mp3(client, monkeypatch):
    """保存用の MP3 を前もって作る働きを、待ち時間なしで動かす(先回り変換は動かさない)。"""
    monkeypatch.setattr(appmod.settings, "prefetch", 1)
    monkeypatch.setattr(appmod.Mp3Maker, "delay", 0.0)
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: None)


def test_mp3_is_prepared_while_listening(client, media, eager_mp3):
    sid = upload(client, media / "aac.m4a")["id"]
    assert render(client, sid, 2)["mp3"] is False
    assert appmod.mp3_maker.wait_idle(30)
    assert rendered() == [f"{sid}_+2.mp3", f"{sid}_+2.wav"]
    assert render(client, sid, 2)["mp3"] is True
    assert render(client, sid, 0)["direct"] is True         # 原曲キーの MP3 も作っておく
    assert appmod.mp3_maker.wait_idle(30)
    assert rendered() == [f"{sid}_+0.mp3", f"{sid}_+2.mp3", f"{sid}_+2.wav"]


def test_mp3_is_not_prepared_when_prefetch_is_off(client, media, eager_mp3, monkeypatch):
    monkeypatch.setattr(appmod.settings, "prefetch", 0)
    sid = upload(client, media / "aac.m4a")["id"]
    render(client, sid, 2)
    assert appmod.mp3_maker.wait_idle(30)
    assert rendered() == [f"{sid}_+2.wav"]


def test_mp3_maker_makes_only_the_latest_key(client, media, monkeypatch):
    """キーを次々に変えている間は作らない。作りかけも、別のキーに変えられたらやめる。"""
    sid = upload(client, media / "aac.m4a")["id"]
    for k in (1, 2, 3):
        appmod.ensure_render(sid, k)
    started = []
    real = audio.encode_mp3

    def encode(src, dst, *, tags_from=None, cancel=None, **kw):
        started.append(dst.name)
        if dst.name.endswith("_+1.mp3"):                    # 終わらないエンコード。取り消されるまで待つ
            assert cancel.wait(10)
            raise audio.RenderCancelled()
        return real(src, dst, tags_from=tags_from, cancel=cancel, **kw)
    monkeypatch.setattr(appmod.audio, "encode_mp3", encode)
    maker = appmod.Mp3Maker()
    try:
        maker.delay = 0.0
        maker.schedule(sid, 1)
        end = time.time() + 10
        while not started and time.time() < end:
            time.sleep(0.01)
        maker.delay = 0.3
        maker.schedule(sid, 2)                              # +1 の作りかけはやめる
        maker.schedule(sid, 3)                              # +2 は始まる前に +3 に置き換わる
        assert maker.wait_idle(30)
        assert started == [f"{sid}_+1.mp3", f"{sid}_+3.mp3"]
        assert [k for k in (1, 2, 3) if appmod.mp3_path(sid, k).exists()] == [3]
        assert appmod._encodes == set()
    finally:
        maker.stop()


def test_mp3_maker_stop_cancels_the_running_encode(client, media, monkeypatch):
    sid = upload(client, media / "aac.m4a")["id"]
    started = threading.Event()

    def encode(src, dst, *, tags_from=None, cancel=None, **kw):
        started.set()
        assert cancel.wait(10)
        raise audio.RenderCancelled()
    monkeypatch.setattr(appmod.audio, "encode_mp3", encode)
    maker = appmod.Mp3Maker()
    maker.delay = 0.0
    maker.schedule(sid, 0)
    assert started.wait(10)
    maker.stop()
    assert maker.wait_idle(10)
    maker._thread.join(10)
    assert not maker._thread.is_alive()


@pytest.fixture()
def slow_mp3(client, media, monkeypatch):
    """原曲キーを聴き始めた状態。MP3 作りは取り消されるまで終わらず、変換は release まで終わらない。"""
    monkeypatch.setattr(appmod.settings, "prefetch", 1)
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: None)
    sid = upload(client, media / "aac.m4a")["id"]
    started, stopped = threading.Event(), threading.Event()

    def encode(src, dst, *, tags_from=None, cancel=None, **kw):
        started.set()
        assert cancel.wait(10)
        stopped.set()
        raise audio.RenderCancelled()
    monkeypatch.setattr(appmod.audio, "encode_mp3", encode)
    fake = FakeRender()
    monkeypatch.setattr(appmod.audio, "render", fake)
    yield sid, fake, started, stopped
    fake.release.set()


def test_choosing_another_key_stops_preparing_the_old_mp3(client, slow_mp3, monkeypatch):
    """別のキーを選んだら、前のキーの MP3 作りはすぐやめる(新しいキーの変換が済むのを待たずに譲る)。"""
    sid, fake, started, stopped = slow_mp3
    monkeypatch.setattr(appmod.Mp3Maker, "delay", 0.0)
    assert render(client, sid, 0)["direct"] is True        # 原曲キーを聴いている。MP3 を作り始める
    assert started.wait(10)
    choose = run_async(client.post, "/api/render", json={"id": sid, "semitones": 5})
    fake.wait_started(5)
    assert stopped.wait(10)                                # +5 の変換はまだ終わっていない
    assert choose["thread"].is_alive()
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    fake.release.set()
    assert finish(choose)["result"].status_code == 200
    assert appmod.mp3_maker.wait_idle(10)


def test_choosing_another_key_drops_the_mp3_not_started_yet(client, slow_mp3, monkeypatch):
    sid, fake, started, stopped = slow_mp3
    monkeypatch.setattr(appmod.Mp3Maker, "delay", 0.3)
    render(client, sid, 0)                                 # 0.3 秒聴いていたら MP3 を作り始める予定
    choose = run_async(client.post, "/api/render", json={"id": sid, "semitones": 5})
    fake.wait_started(5)
    time.sleep(0.6)
    assert not started.is_set()                            # 別のキーに移ったので、作り始めない
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    fake.release.set()
    assert finish(choose)["result"].status_code == 200


def test_asking_for_the_same_key_again_keeps_preparing_its_mp3(client, slow_mp3, monkeypatch):
    sid, fake, started, stopped = slow_mp3
    monkeypatch.setattr(appmod.Mp3Maker, "delay", 0.0)
    render(client, sid, 0)
    assert started.wait(10)
    render(client, sid, 0)                                 # 同じキーをもう一度(画面を開き直したときなど)
    render(client, sid, 0, peek=True)
    time.sleep(0.2)
    assert not stopped.is_set()
    appmod.mp3_maker.stop()                                # 片付け(終わらない MP3 作りをやめさせる)
    assert stopped.wait(10)


@pytest.fixture()
def saving(client, tone_dir, monkeypatch):
    """+3 は変換済み。+5 への変換は release まで、MP3 作りは go まで終わらない。"""
    sid = upload_tone(client, tone_dir)
    fake = FakeRender(blocking={5})
    monkeypatch.setattr(appmod.audio, "render", fake)
    encoding, go = threading.Event(), threading.Event()

    def encode(src, dst, *, tags_from=None, cancel=None, **kw):
        encoding.set()
        assert go.wait(10)
        dst.write_bytes(b"mp3")
    monkeypatch.setattr(appmod.audio, "encode_mp3", encode)
    appmod.ensure_render(sid, 3)
    yield sid, fake, encoding, go
    go.set()
    fake.release.set()


@pytest.mark.parametrize("request_mp3", [
    lambda c, sid: c.post("/api/mp3", json={"id": sid, "semitones": 3}),
    lambda c, sid: c.get(f"/api/audio/{sid}?semitones=3&download=1")])
def test_saving_takes_priority_over_prefetch(client, saving, request_mp3):
    """「ダウンロード」を押した人を待たせないよう、MP3 を作っている間は先回り変換に譲ってもらう。"""
    sid, fake, encoding, go = saving
    bg = run_async(appmod.ensure_render, sid, 5, background=True)
    fake.wait_started(5)
    save = run_async(request_mp3, client, sid)
    assert isinstance(finish(bg).get("error"), audio.RenderCancelled)     # 動いていた先回り変換は取り消す
    assert encoding.wait(10)
    late = run_async(appmod.ensure_render, sid, 6, background=True)
    assert isinstance(finish(late, 2).get("error"), audio.RenderCancelled)  # 作っている間は始めない
    go.set()
    assert finish(save)["result"].status_code == 200
    assert appmod._foreground == 0 and appmod._renders == [] and appmod._encodes == set()
    again = run_async(appmod.ensure_render, sid, 6, background=True)     # 済んだら、先回り変換はまた動ける
    assert finish(again)["result"] == appmod.render_path(sid, 6)


def test_preparing_an_mp3_in_advance_does_not_stop_prefetch(client, saving):
    """聴いている間に前もって作る MP3 は急ぎではないので、先回り変換を止めない。"""
    sid, fake, encoding, go = saving
    bg = run_async(appmod.ensure_render, sid, 5, background=True)
    fake.wait_started(5)
    go.set()
    assert appmod.ensure_mp3(sid, 3, background=True) == appmod.mp3_path(sid, 3)
    assert bg["thread"].is_alive() and "error" not in bg
    assert appmod._foreground == 0
    fake.release.set()
    assert finish(bg)["result"] == appmod.render_path(sid, 5)


def test_saving_gives_way_back_even_when_it_fails(client, saving, monkeypatch):
    sid, fake, encoding, go = saving

    def broken(src, dst, **kw):
        raise audio.AudioError("boom")
    monkeypatch.setattr(appmod.audio, "encode_mp3", broken)
    assert client.post("/api/mp3", json={"id": sid, "semitones": 3}).status_code == 500
    assert appmod._foreground == 0 and appmod._encodes == set()
    assert client.post("/api/mp3", json={"id": sid, "semitones": 3}).status_code == 500
    assert appmod._foreground == 0


# ---------------------------------------------------------------- 変換済みかどうかの問い合わせ(peek)
def test_peek_answers_only_when_the_key_is_ready(client, media, monkeypatch):
    """peek は「変換済みならすぐ切り替える」ための問い合わせ。変換も先回りも始めない。"""
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    seen = []
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: seen.append(keys))
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: seen.append("mp3"))
    sid = upload(client, media / "tone440.mp3")["id"]
    assert render(client, sid, 4, peek=True) == {"ready": False}
    assert rendered() == [] and seen == [] and appmod._renders == []
    assert render(client, sid, 4)["ready"] is True
    seen.clear()
    body = render(client, sid, 4, peek=True)
    assert body["ready"] is True and body["url"] == f"/api/audio/{sid}?semitones=4"
    assert seen == [[5, 3, 6, 2], "mp3"]                    # 切り替わったので、その近くを先回りする
    # 原曲キー: そのまま再生できる音源ならいつでもすぐ再生できる。そうでなければ変換が要る
    direct = upload(client, media / "aac.m4a")["id"]
    assert render(client, direct, 0, peek=True)["ready"] is True
    assert render(client, direct, 0, peek=True, compat=True) == {"ready": False}
    assert render(client, sid, 0, peek=True) == {"ready": False}
    r = client.post("/api/render", json={"id": "0" * 20, "semitones": 1, "peek": True})
    assert r.status_code == 404
    r = client.post("/api/render", json={"id": sid, "semitones": 13, "peek": True})
    assert r.status_code == 400


# ---------------------------------------------------------------- 読み込みの完了を待つ
def test_job_wait_returns_as_soon_as_the_download_finishes(client, monkeypatch):
    gate = threading.Event()

    def slow(url, progress=None):
        gate.wait(10)
        return {"id": "a" * 20, "title": "t", "duration": 1.0}
    monkeypatch.setattr(appmod, "download_url", slow)
    job = client.post("/api/load", json={"url": "https://example.com/a.mp3"}).json()["job"]
    t = time.monotonic()
    assert client.get(f"/api/jobs/{job}?wait=0.3").json()["status"] == "running"
    assert 0.25 < time.monotonic() - t < 5                  # 終わっていなければ、言われた時間だけ待つ
    threading.Timer(0.3, gate.set).start()
    t = time.monotonic()
    j = client.get(f"/api/jobs/{job}?wait=20").json()
    assert j["status"] == "done" and j["result"]["id"] == "a" * 20
    assert time.monotonic() - t < 10                        # 終わったらすぐ返る(20 秒は待たない)
    assert client.get(f"/api/jobs/{job}").json()["status"] == "done"
    assert client.get(f"/api/jobs/{job}?wait=31").status_code == 422
    assert client.get(f"/api/jobs/{job}?wait=-1").status_code == 422


def test_job_wait_reports_errors(client, monkeypatch):
    def fail(url, progress=None):
        raise appmod.UserError("だめでした")
    monkeypatch.setattr(appmod, "download_url", fail)
    job = client.post("/api/load", json={"url": "https://example.com/a.mp3"}).json()["job"]
    j = client.get(f"/api/jobs/{job}?wait=10").json()
    assert j["status"] == "error" and j["error"] == "だめでした"


# ---------------------------------------------------------------- キャッシュの片付け
def test_cache_is_tidied_after_each_render(client, media, monkeypatch):
    """変換結果(WAV)は大きいので、上限を超えたら変換のたびに古いものから消す。いま使うものは消さない。"""
    import os
    sid = upload(client, media / "tone440.mp3")["id"]
    render(client, sid, 1)
    old = time.time() - 100
    os.utime(appmod.render_path(sid, 1), (old, old))
    monkeypatch.setattr(appmod.settings, "max_cache_bytes", 0)
    render(client, sid, 2)
    assert rendered() == [f"{sid}_+2.wav"]                  # +1 は消え、いま変換した +2 は残る
    assert appmod.source_path(sid) is not None              # 音源も残る
    assert client.get(f"/api/audio/{sid}?semitones=2").status_code == 200
    a = client.get(f"/api/audio/{sid}?semitones=2&download=1")
    assert a.status_code == 200 and appmod.mp3_path(sid, 2).exists()


def test_cleanup_protects_old_files_in_use(client, media, monkeypatch):
    import os
    sid = upload(client, media / "tone440.mp3")["id"]
    render(client, sid, 1)
    week = time.time() - 30 * 24 * 3600
    for p in [appmod.render_path(sid, 1), *(appmod.sources_dir() / sid).iterdir()]:
        os.utime(p, (week, week))
    appmod.cleanup_cache(protect=[appmod.render_path(sid, 1), appmod.sources_dir() / sid])
    assert rendered() == [f"{sid}_+1.wav"] and appmod.source_path(sid) is not None
    appmod.cleanup_cache()
    assert rendered() == [] and appmod.source_path(sid) is None


def test_tidy_cache_skips_when_another_cleanup_is_running(client, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "cleanup_cache", lambda **kw: calls.append(kw))
    appmod.tidy_cache()
    assert len(calls) == 1
    with appmod._cleanup_lock:
        appmod.tidy_cache()
    assert len(calls) == 1

    def fail(**kw):
        raise OSError("disk")
    monkeypatch.setattr(appmod, "cleanup_cache", fail)
    appmod.tidy_cache()                                     # 例外にならない
    assert appmod._cleanup_lock.acquire(blocking=False)
    appmod._cleanup_lock.release()


# ---------------------------------------------------------------- YouTube の読み込み(yt-dlp の使い回し)
YT = "https://www.youtube.com/watch?v="


@pytest.fixture()
def fake_ydl(client, monkeypatch, media):
    """yt-dlp の代わり。extract_info は決まった情報を返し、process_ie_result は音声ファイルを置く。"""
    import yt_dlp

    class Fake:
        instances = []
        fail_extract = None     # (インスタンス, URL) を受け取り、例外を返すと extract_info がそれを送出する
        fail_download = None    # 同じく process_ie_result 用

        def __init__(self, opts):
            self.opts, self.extracted, self.downloaded, self.closed = opts, [], [], False
            Fake.instances.append(self)

        def __enter__(self): return self
        def __exit__(self, *a): self.close()
        def close(self): self.closed = True

        def extract_info(self, url, download=False):
            assert download is False
            self.extracted.append(url)
            if Fake.fail_extract and (e := Fake.fail_extract(self, url)):
                raise e
            return {"id": url.rsplit("=", 1)[-1], "title": "テスト曲", "duration": 5.0, "webpage_url": url}

        def process_ie_result(self, info, download=True):
            assert download is True
            self.downloaded.append(info["webpage_url"])
            if Fake.fail_download and (e := Fake.fail_download(self, info["webpage_url"])):
                raise e
            shutil.copyfile(media / "aac.m4a", self.opts["outtmpl"].replace("%(ext)s", "m4a"))
            return info
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
    return Fake


def summary(fake):
    return [(len(i.extracted), len(i.downloaded)) for i in fake.instances]


def test_youtube_extractor_is_reused(client, fake_ydl):
    """YouTube の情報を調べる yt-dlp は使い回す(2 曲目から速い)。ダウンロードは毎回新しいもので行う。"""
    j = load(client, YT + "one")
    assert j["status"] == "done", j
    assert j["result"]["title"] == "テスト曲" and j["result"]["direct"] == "audio/mp4"
    assert summary(fake_ydl) == [(1, 0), (0, 1)]            # 調べる用とダウンロード用
    keeper, first = fake_ydl.instances
    assert "outtmpl" not in keeper.opts and not keeper.closed
    assert keeper.opts["format"] == first.opts["format"] == "bestaudio[audio_channels<=?2]/bestaudio/best"
    assert first.opts["concurrent_fragment_downloads"] == 4 and first.closed
    assert load(client, YT + "two")["status"] == "done"
    assert summary(fake_ydl) == [(2, 0), (0, 1), (0, 1)]    # 調べる用は同じものを使った
    assert keeper.extracted == [YT + "one", YT + "two"]
    # 読み込み済みの URL は、何もせずにすぐ返る
    assert load(client, YT + "one")["result"]["id"] == j["result"]["id"]
    assert len(fake_ydl.instances) == 3


def test_youtube_stale_extractor_is_replaced(client, fake_ydl):
    """使い回していた yt-dlp で失敗したら、新しく作り直してもう一度試す。"""
    import yt_dlp
    assert load(client, YT + "one")["status"] == "done"
    keeper = fake_ydl.instances[0]
    fake_ydl.fail_extract = lambda inst, url: (
        yt_dlp.utils.DownloadError("ERROR: stale session") if inst is keeper else None)
    assert load(client, YT + "two")["status"] == "done"
    assert summary(fake_ydl) == [(2, 0), (0, 1), (1, 1)]    # 3 つ目が、調べるところからやり直した
    assert keeper.closed and appmod._extractor._ydl is None  # 失敗したものは捨てる
    fake_ydl.fail_extract = None
    assert load(client, YT + "three")["status"] == "done"
    assert summary(fake_ydl)[3:] == [(1, 0), (0, 1)]        # 次からはまた使い回す


def test_youtube_error_on_a_new_extractor_is_not_retried(client, fake_ydl):
    """作ったばかりの yt-dlp で失敗したら、それが答え(同じことを繰り返して待たせない)。"""
    import yt_dlp
    fake_ydl.fail_extract = lambda inst, url: yt_dlp.utils.DownloadError("ERROR: [youtube] x: Video unavailable")
    j = load(client, YT + "gone")
    assert j["status"] == "error" and "利用できません" in j["error"]
    assert summary(fake_ydl) == [(1, 0)]
    assert appmod._extractor._ydl is None


def test_youtube_download_failure_falls_back_to_one_instance(client, fake_ydl):
    import yt_dlp
    fake_ydl.fail_download = lambda inst, url: (
        yt_dlp.utils.DownloadError("ERROR: unable to download") if inst is fake_ydl.instances[1] else None)
    assert load(client, YT + "one")["status"] == "done"
    assert summary(fake_ydl) == [(1, 0), (0, 1), (1, 1)]


def test_youtube_extractor_in_use_does_not_block_another_load(client, fake_ydl):
    with appmod._extractor._lock:                           # ほかの読み込みが使っている最中
        assert load(client, YT + "one")["status"] == "done"
    assert summary(fake_ydl) == [(1, 1)]                    # 待たずに、これまでどおり 1 つで済ませた


def test_youtube_limits_are_checked_before_downloading(client, fake_ydl, monkeypatch):
    monkeypatch.setattr(appmod.settings, "max_duration", 2)
    j = load(client, YT + "long")
    assert j["status"] == "error" and "長すぎます" in j["error"]
    assert summary(fake_ydl) == [(1, 0)]


def test_other_sites_do_not_use_the_shared_extractor(client, http_server):
    assert load(client, f"{http_server}/tone440.mp3")["status"] == "done"
    assert appmod._extractor._ydl is None


def test_extractor_warm_up_prepares_without_fetching(fake_ydl):
    ex = appmod._Extractor()
    REAL_WARM(ex)
    assert summary(fake_ydl) == [(0, 0)]                    # 用意するだけ(通信しない)
    info, reused = ex.extract(YT + "one", appmod._YdlLogger())
    assert info["id"] == "one" and reused is False          # 用意しただけのものは「使い回し」ではない
    assert ex.extract(YT + "two", appmod._YdlLogger())[1] is True
    assert len(fake_ydl.instances) == 1
    ex.close()
    assert fake_ydl.instances[0].closed


def test_extractor_collects_warnings_per_load(fake_ydl):
    ex = appmod._Extractor()
    first, second = appmod._YdlLogger(), appmod._YdlLogger()
    real = fake_ydl.extract_info

    def noisy(self, url, download=False):
        self.opts["logger"].warning("warn " + url)
        return real(self, url, download)
    fake_ydl.extract_info = noisy
    ex.extract("u=1", first)
    ex.extract("u=2", second)
    assert first.warnings == ["warn u=1"] and second.warnings == ["warn u=2"]


@pytest.fixture(scope="session")
def hls_url(tone_dir, http_server):
    """細切れ(HLS)で配信される音声。2 秒ごとのファイル 7 個と、その一覧(m3u8)。"""
    d = tone_dir / "hls"
    d.mkdir()
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(tone_dir / "song.wav"),
                    "-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    str(d / "song.m3u8")], check=True)
    assert len(list(d.glob("*.ts"))) >= 5
    return f"{http_server}/hls/song.m3u8"


def test_load_fragmented_stream(client, hls_url, tmp_path):
    """細切れで配信される音声は、並行してダウンロードする。結果はこれまでと同じ 1 つの音声になる。"""
    j = load(client, hls_url)
    assert j["status"] == "done", j
    assert j["result"]["duration"] == pytest.approx(14.0, abs=0.3)
    sid = j["result"]["id"]
    a = client.get(render(client, sid, 0)["url"])
    assert a.status_code == 200
    out = tmp_path / "play.bin"
    out.write_bytes(a.content)
    assert dominant_freq(decode(out)) == pytest.approx(330, rel=0.02)
    assert len(decode(out)) / 44100 == pytest.approx(14.0, abs=0.3)   # 抜けている区間が無い


def test_load_fragmented_stream_respects_the_size_limit(client, hls_url, monkeypatch):
    monkeypatch.setattr(appmod.settings, "max_filesize", 100_000)  # 1 個ずつは収まるが、合計は超える
    j = load(client, hls_url)
    assert j["status"] == "error" and "大きすぎ" in j["error"], j
    assert not [p for p in appmod.sources_dir().iterdir() if p.name.startswith(".tmp-")]


def test_progress_never_goes_backwards(client, monkeypatch):
    gate = threading.Event()

    def download(url, progress=None):
        for p in (0.2, 0.5, 0.4, 0.45):
            progress(p)
        gate.wait(10)
        return {"id": "a" * 20, "title": "t", "duration": 1.0}
    monkeypatch.setattr(appmod, "download_url", download)
    job = client.post("/api/load", json={"url": "https://example.com/a.mp3"}).json()["job"]
    end = time.time() + 10
    while client.get(f"/api/jobs/{job}").json()["progress"] is None and time.time() < end:
        time.sleep(0.01)
    time.sleep(0.1)
    assert client.get(f"/api/jobs/{job}").json()["progress"] == 0.5
    gate.set()
    assert client.get(f"/api/jobs/{job}?wait=10").json()["progress"] == 1.0


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=abc", True), ("https://youtu.be/abc", True),
    ("https://music.youtube.com/watch?v=abc", True), ("https://m.youtube.com/watch?v=abc", True),
    ("https://www.youtube-nocookie.com/embed/abc", True), ("HTTPS://WWW.YOUTUBE.COM/watch?v=abc", True),
    ("https://example.com/youtube.com/a.mp3", False), ("https://notyoutube.com/a", False),
    ("https://youtube.com.example.org/a", False), ("https://soundcloud.com/a/b", False)])
def test_is_youtube(url, expected):
    assert appmod._is_youtube(url) is expected


# ---------------------------------------------------------------- 先回り変換・取り消し
def upload_tone(client, tone_dir, name="tone440.mp3"):
    """テスト用の音源を読み込む。MP3 は原曲キーも変換が要り、WAV はそのまま再生できる。"""
    with open(tone_dir / name, "rb") as f:
        r = client.post("/api/upload", files={"file": (name, f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def rendered_keys(sid):
    return sorted(int(p.stem.split("_")[1]) for p in appmod.renders_dir().glob(f"{sid}_*.wav"))


def test_prefetch_renders_nearby_keys(client, tone_dir, monkeypatch):
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    sid = upload_tone(client, tone_dir)
    assert client.post("/api/render", json={"id": sid, "semitones": 2}).status_code == 200
    assert appmod.prefetcher.wait_idle(60)
    assert rendered_keys(sid) == [0, 1, 2, 3, 4]            # 2 つ隣まで
    # 先回りした分はそのまま使われ、さらにその先を用意する
    assert client.post("/api/render", json={"id": sid, "semitones": 3}).status_code == 200
    assert appmod.prefetcher.wait_idle(60)
    assert rendered_keys(sid) == [0, 1, 2, 3, 4, 5]
    out = decode(appmod.render_path(sid, 5))
    assert dominant_freq(out) == pytest.approx(440 * 2 ** (5 / 12), rel=0.02)


def test_prefetch_order_follows_the_direction(client, tone_dir, monkeypatch):
    """近いキーから、動かしている向きを先に用意する。もうあるものは頼まない。"""
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    seen = []
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: seen.append(keys))
    sid = upload_tone(client, tone_dir)
    assert client.post("/api/render", json={"id": sid, "semitones": 2}).status_code == 200
    assert seen[-1] == [3, 1, 4, 0]                         # 最初は上から
    assert client.post("/api/render", json={"id": sid, "semitones": 1}).status_code == 200
    assert seen[-1] == [0, -1, 3]                           # 下げたので下を先に(+2 はもうある)
    assert client.post("/api/render", json={"id": sid, "semitones": 2}).status_code == 200
    assert seen[-1] == [3, 4, 0]                            # 上げたので上を先に(+1 はもうある)
    monkeypatch.setattr(appmod.settings, "prefetch", 1)
    assert client.post("/api/render", json={"id": sid, "semitones": 2}).status_code == 200
    assert seen[-1] == [3]                                  # 隣だけ
    monkeypatch.setattr(appmod.settings, "prefetch", 3)
    assert client.post("/api/render", json={"id": sid, "semitones": 2}).status_code == 200
    assert seen[-1] == [3, 4, 0, 5, -1]


def test_prefetch_skips_the_original_key_when_the_source_plays_as_is(client, tone_dir, monkeypatch):
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    seen = []
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: seen.append(keys))
    sid = upload_tone(client, tone_dir, "tone440.wav")
    assert client.post("/api/render", json={"id": sid, "semitones": 1}).status_code == 200
    assert seen[-1] == [2, 3, -1]                           # 0 は変換しなくても再生できる
    # そのままでは再生できなかったブラウザ(compat)には、0 も用意する
    assert client.post("/api/render", json={"id": sid, "semitones": 1, "compat": True}).status_code == 200
    assert seen[-1] == [2, 0, 3, -1]


def test_prefetch_is_narrower_for_long_sources(client, tone_dir, monkeypatch):
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    monkeypatch.setattr(appmod.settings, "prefetch_long", 3)  # 5 秒の音源を「長い」扱いにする
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    seen = []
    monkeypatch.setattr(appmod.prefetcher, "schedule", lambda source_id, keys: seen.append(keys))
    sid = upload_tone(client, tone_dir)
    assert client.post("/api/render", json={"id": sid, "semitones": 5}).status_code == 200
    assert seen == [[6, 4]]


def test_prefetch_stays_in_range_and_can_be_disabled(client, tone_dir, monkeypatch):
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    sid = upload_tone(client, tone_dir)
    assert client.post("/api/render", json={"id": sid, "semitones": 5}).status_code == 200
    assert appmod.prefetcher.wait_idle(60)
    assert rendered_keys(sid) == [5]                      # prefetch=0 なら何もしない
    monkeypatch.setattr(appmod.settings, "prefetch", 2)
    assert client.post("/api/render", json={"id": sid, "semitones": 12}).status_code == 200
    assert appmod.prefetcher.wait_idle(60)
    assert rendered_keys(sid) == [5, 10, 11, 12]          # +13, +14 は作らない


@pytest.mark.parametrize("value,span", [(None, 2), ("", 2), ("0", 0), ("1", 1), ("2", 2), ("3", 3),
                                        ("9", 3), ("-1", 0), ("abc", 2), (" 1 ", 1)])
def test_prefetch_span_from_environment(monkeypatch, value, span):
    if value is None:
        monkeypatch.delenv("KEYCHANGER_PREFETCH", raising=False)
    else:
        monkeypatch.setenv("KEYCHANGER_PREFETCH", value)
    assert appmod._prefetch_span() == span


class FakeRender:
    """audio.render の代わり。release されるまで終わらず、その間に cancel されたら RenderCancelled。"""

    def __init__(self, blocking=None):
        self.blocking = blocking  # 止めておくキー(None = すべて)
        self.started = {}   # semitones -> Event
        self.release = threading.Event()
        self.lock = threading.Lock()

    def wait_started(self, semitones, timeout=10):
        with self.lock:
            ev = self.started.setdefault(semitones, threading.Event())
        assert ev.wait(timeout), f"render {semitones} did not start"

    def __call__(self, src, dst, semitones, engine=None, jobs=None, cancel=None):
        with self.lock:
            self.started.setdefault(semitones, threading.Event()).set()
        while not self.release.is_set() and (self.blocking is None or semitones in self.blocking):
            if cancel.wait(0.01):
                raise audio.RenderCancelled()
        dst.write_bytes(b"wav")
        return "fake"


def run_async(fn, *a, **kw):
    box = {}

    def target():
        try:
            box["result"] = fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    box["thread"] = t
    return box


def finish(box, timeout=10):
    box["thread"].join(timeout)
    assert not box["thread"].is_alive()
    return box


@pytest.fixture()
def fake_render(client, tone_dir, monkeypatch):
    fake = FakeRender()
    sid = upload_tone(client, tone_dir)
    monkeypatch.setattr(appmod.audio, "render", fake)
    yield fake, sid
    fake.release.set()


def test_background_render_yields_to_foreground(fake_render):
    fake, sid = fake_render
    bg = run_async(appmod.ensure_render, sid, 5, background=True)
    fake.wait_started(5)
    fg = run_async(appmod.ensure_render, sid, 6)
    # 利用者の変換が来たら、先回り変換は取り消される
    assert isinstance(finish(bg).get("error"), audio.RenderCancelled)
    fake.wait_started(6)
    # 利用者の変換が動いている間は、先回り変換を始めない
    late = run_async(appmod.ensure_render, sid, 7, background=True)
    assert isinstance(finish(late, 2).get("error"), audio.RenderCancelled)
    fake.release.set()
    assert finish(fg)["result"] == appmod.render_path(sid, 6)
    assert not appmod.render_path(sid, 5).exists()
    assert appmod._renders == [] and appmod._foreground == 0


def test_foreground_waits_for_background_render_of_same_key(fake_render):
    fake, sid = fake_render
    bg = run_async(appmod.ensure_render, sid, 5, background=True)
    fake.wait_started(5)
    fg = run_async(appmod.ensure_render, sid, 5)
    time.sleep(0.2)
    assert "error" not in bg and bg["thread"].is_alive()   # 同じキーなら取り消さずに待つ
    fake.release.set()
    assert finish(bg)["result"] == finish(fg)["result"] == appmod.render_path(sid, 5)


def test_newer_key_supersedes_running_render(fake_render):
    fake, sid = fake_render
    first = run_async(appmod.ensure_render, sid, 1, supersede=True)
    fake.wait_started(1)
    other = run_async(appmod.ensure_render, sid, 3)        # 再生・ダウンロード側(supersede なし)
    fake.wait_started(3)
    time.sleep(0.1)
    assert first["thread"].is_alive()                      # supersede なしでは取り消さない
    second = run_async(appmod.ensure_render, sid, 2, supersede=True)
    assert isinstance(finish(first).get("error"), audio.RenderCancelled)
    assert isinstance(finish(other).get("error"), audio.RenderCancelled)
    fake.wait_started(2)
    fake.release.set()
    assert finish(second)["result"] == appmod.render_path(sid, 2)
    assert appmod._renders == [] and appmod._foreground == 0


def test_superseded_render_returns_409(client, tone_dir, monkeypatch):
    sid = upload_tone(client, tone_dir)

    def cancelled(*a, **kw):
        raise audio.RenderCancelled()
    monkeypatch.setattr(appmod.audio, "render", cancelled)
    r = client.post("/api/render", json={"id": sid, "semitones": 4})
    assert r.status_code == 409 and r.json()["detail"]
    assert appmod._renders == [] and appmod._foreground == 0


def test_playing_does_not_supersede_but_choosing_a_key_does(client, fake_render):
    """/api/audio(再生・ダウンロード)はほかの変換を取り消さず、/api/render(キーを選ぶ)は取り消す。"""
    fake, sid = fake_render
    first = run_async(appmod.ensure_render, sid, 1, supersede=True)
    fake.wait_started(1)
    play = run_async(client.get, f"/api/audio/{sid}?semitones=3")
    fake.wait_started(3)
    time.sleep(0.1)
    assert first["thread"].is_alive()
    choose = run_async(client.post, "/api/render", json={"id": sid, "semitones": 2})
    assert isinstance(finish(first).get("error"), audio.RenderCancelled)
    assert finish(play)["result"].status_code == 409
    fake.release.set()
    assert finish(choose)["result"].status_code == 200


@pytest.fixture()
def fake_prefetch(client, tone_dir, monkeypatch):
    """先回り変換(隣だけ)を有効にし、+1 への変換だけ release されるまで終わらないようにする。"""
    monkeypatch.setattr(appmod.settings, "prefetch", 1)
    monkeypatch.setattr(appmod.mp3_maker, "schedule", lambda source_id, semitones: None)
    sid = upload_tone(client, tone_dir)
    fake = FakeRender(blocking={1})
    monkeypatch.setattr(appmod.audio, "render", fake)
    yield fake, sid
    fake.release.set()
    appmod.prefetcher.wait_idle(10)


def test_prefetch_is_replaced_when_the_key_moves_on(client, fake_prefetch):
    fake, sid = fake_prefetch
    assert client.post("/api/render", json={"id": sid, "semitones": 0}).status_code == 200
    fake.wait_started(1)                                   # +1 を先回り中(まだ終わらない)
    assert client.post("/api/render", json={"id": sid, "semitones": 5}).status_code == 200
    assert appmod.prefetcher.wait_idle(10)
    # +1 と -1 の先回りはやめて、新しいキーの隣(+6, +4)を用意する
    assert rendered_keys(sid) == [0, 4, 5, 6]


def test_prefetch_resumes_after_yielding(client, fake_prefetch):
    fake, sid = fake_prefetch
    assert client.post("/api/render", json={"id": sid, "semitones": 0}).status_code == 200
    fake.wait_started(1)
    fake.started.clear()
    appmod.ensure_render(sid, 7)                           # 利用者の変換(再生・ダウンロード)が割り込む
    fake.wait_started(1)                                   # 譲ったあと、+1 の先回りをやり直す
    fake.release.set()
    assert appmod.prefetcher.wait_idle(10)
    assert rendered_keys(sid) == [-1, 0, 1, 7]
