"""起動まわり(待ち受けポートの選び方、Windows 用バッチファイル)のテスト。"""
import errno
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

import app as appmod

ROOT = Path(appmod.__file__).resolve().parent


# ---------------------------------------------------------------- 道具
def listen_on(port=0):
    """127.0.0.1 の port で待ち受けて「ほかのアプリが使用中」の状態を作る。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen()
    return s


@pytest.fixture()
def busy_port():
    s = listen_on()
    yield s.getsockname()[1]
    s.close()


@pytest.fixture()
def free_port():
    s = listen_on()
    port = s.getsockname()[1]
    s.close()
    return port


def port_of(sock):
    return sock.getsockname()[1]


def accepts_connections(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def in_use(port=0):
    return OSError(errno.EADDRINUSE, "Address already in use")


class FakeBind:
    """_bind の代わり。fail(port) が例外を返したポートは開けないことにする。"""

    def __init__(self, fail):
        self.fail = fail
        self.tried = []

    def __call__(self, host, port):
        self.tried.append(port)
        exc = self.fail(port)
        if exc:
            raise exc
        return ("socket", host, port)


# ---------------------------------------------------------------- ポート選び
def test_uses_the_requested_port_when_it_is_free(free_port):
    sock = appmod.open_server_socket("127.0.0.1", free_port, fallback=True)
    try:
        assert port_of(sock) == free_port
        assert accepts_connections(free_port)
    finally:
        sock.close()


def test_moves_to_another_port_when_the_requested_one_is_busy(busy_port):
    sock = appmod.open_server_socket("127.0.0.1", busy_port, fallback=True)
    try:
        assert port_of(sock) != busy_port
        assert accepts_connections(port_of(sock))
    finally:
        sock.close()


def test_two_instances_get_different_ports(free_port):
    a = appmod.open_server_socket("127.0.0.1", free_port, fallback=True)
    b = appmod.open_server_socket("127.0.0.1", free_port, fallback=True)
    try:
        assert port_of(a) == free_port
        assert port_of(b) != free_port
    finally:
        a.close()
        b.close()


def test_without_fallback_a_busy_port_is_an_error(busy_port):
    with pytest.raises(appmod.PortInUse) as e:
        appmod.open_server_socket("127.0.0.1", busy_port, fallback=False)
    assert e.value.port == busy_port


def test_tries_the_following_ports_in_order_then_lets_the_os_choose(monkeypatch):
    fake = FakeBind(lambda port: in_use() if port else None)  # 0(OS 任せ)だけ成功
    monkeypatch.setattr(appmod, "_bind", fake)
    assert appmod.open_server_socket("127.0.0.1", 8000, fallback=True) == ("socket", "127.0.0.1", 0)
    assert fake.tried == list(range(8000, 8000 + appmod.PORT_TRIES)) + [0]


def test_stops_at_the_first_free_port(monkeypatch):
    fake = FakeBind(lambda port: in_use() if port < 8003 else None)
    monkeypatch.setattr(appmod, "_bind", fake)
    assert appmod.open_server_socket("127.0.0.1", 8000, fallback=True)[2] == 8003
    assert fake.tried == [8000, 8001, 8002, 8003]


def test_without_fallback_only_the_requested_port_is_tried(monkeypatch):
    fake = FakeBind(lambda port: in_use())
    monkeypatch.setattr(appmod, "_bind", fake)
    with pytest.raises(appmod.PortInUse):
        appmod.open_server_socket("127.0.0.1", 8000, fallback=False)
    assert fake.tried == [8000]


def test_port_in_use_when_nothing_at_all_is_free(monkeypatch):
    monkeypatch.setattr(appmod, "_bind", FakeBind(lambda port: in_use()))
    with pytest.raises(appmod.PortInUse) as e:
        appmod.open_server_socket("127.0.0.1", 8000, fallback=True)
    assert e.value.port == 8000


def test_a_port_windows_refuses_access_to_counts_as_busy(monkeypatch):
    # Windows は予約済み・占有中のポートを WSAEACCES(アクセス拒否)で断ることがある
    fake = FakeBind(lambda port: PermissionError(errno.EACCES, "forbidden") if port == 8000 else None)
    monkeypatch.setattr(appmod, "_bind", fake)
    assert appmod.open_server_socket("127.0.0.1", 8000, fallback=True)[2] == 8001


def test_other_errors_are_not_mistaken_for_a_busy_port(monkeypatch):
    fake = FakeBind(lambda port: OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"))
    monkeypatch.setattr(appmod, "_bind", fake)
    with pytest.raises(OSError) as e:
        appmod.open_server_socket("192.0.2.1", 8000, fallback=True)
    assert not isinstance(e.value, appmod.PortInUse)
    assert fake.tried == [8000]  # 番号を変えても直らないので、次は試さない


def test_never_tries_a_port_number_above_65535(monkeypatch):
    fake = FakeBind(lambda port: in_use() if port else None)
    monkeypatch.setattr(appmod, "_bind", fake)
    appmod.open_server_socket("127.0.0.1", 65530, fallback=True)
    assert fake.tried == [65530, 65531, 65532, 65533, 65534, 65535, 0]


# ---------------------------------------------------------------- main()
class Launched:
    """main() を、サーバーとブラウザを本物の代わりに記録役へ差し替えて動かした結果。"""

    def __init__(self):
        self.ports = []        # サーバーに渡されたソケットのポート番号
        self.listening = []    # そのとき実際に接続を受け付けていたか
        self.opened = []       # ブラウザで開いた URL
        self.browser = threading.Event()


@pytest.fixture()
def launch(monkeypatch, capsys):
    rec = Launched()

    def fake_run(self, sockets=None):
        for s in sockets or []:
            rec.ports.append(port_of(s))
            rec.listening.append(accepts_connections(port_of(s)))
        if rec.expect_browser:
            rec.browser.wait(5)  # ブラウザを開く処理(別スレッド)が済むまで「動いている」ことにする
        self.started = True

    def fake_open(url):
        rec.opened.append(url)
        rec.browser.set()

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(appmod.webbrowser, "open", fake_open)
    monkeypatch.setattr(appmod, "BROWSER_DELAY", 0)
    monkeypatch.setattr(appmod, "_app_url", appmod._app_url)  # テスト後に元へ戻す
    monkeypatch.delenv("KEYCHANGER_PORT", raising=False)
    monkeypatch.delenv("KEYCHANGER_HOST", raising=False)

    def run(*argv, default_port=None, env_port=None):
        if default_port is not None:
            monkeypatch.setattr(appmod, "DEFAULT_PORT", default_port)
        if env_port is not None:
            monkeypatch.setenv("KEYCHANGER_PORT", str(env_port))
        rec.code = 0
        rec.expect_browser = "--open" in argv
        try:
            appmod.main(list(argv))
        except SystemExit as e:
            rec.code = e.code
        rec.out = capsys.readouterr().out
        return rec

    return run


def test_main_starts_on_the_default_port_when_it_is_free(launch, free_port):
    r = launch("--open", default_port=free_port)
    assert r.code == 0
    assert r.ports == [free_port] and r.listening == [True]
    assert r.opened == [f"http://127.0.0.1:{free_port}"]
    assert f"http://127.0.0.1:{free_port} を開いてください" in r.out
    assert "使用中" not in r.out


def test_main_moves_to_a_free_port_when_the_default_is_busy(launch, busy_port):
    r = launch("--open", default_port=busy_port)
    assert r.code == 0
    (port,) = r.ports
    assert port != busy_port and r.listening == [True]
    # 画面の案内もブラウザも、実際に起動した番号を指している(別のアプリを開かない)
    assert r.opened == [f"http://127.0.0.1:{port}"]
    assert f"http://127.0.0.1:{port} を開いてください" in r.out
    assert f"http://127.0.0.1:{busy_port} " not in r.out
    assert f"ポート {busy_port} はほかのアプリが使用中" in r.out


def test_main_does_not_open_a_browser_unless_asked(launch, free_port):
    r = launch(default_port=free_port)
    time.sleep(0.2)
    assert r.code == 0 and r.ports == [free_port] and r.opened == []


def test_main_lets_the_os_choose_when_port_zero_is_requested(launch):
    r = launch("--port", "0", "--open")
    assert r.code == 0
    (port,) = r.ports
    assert port > 0 and r.opened == [f"http://127.0.0.1:{port}"]
    assert "使用中" not in r.out


@pytest.mark.parametrize("how", ["flag", "env"])
def test_main_rejects_an_impossible_port_number_without_a_traceback(launch, how):
    # 範囲外の番号は OS によっては黙って別の番号(70000 → 4464)に読み替えられてしまう
    r = launch("--port", "70000") if how == "flag" else launch(env_port=70000)
    assert r.code == 1 and r.ports == []
    assert "70000" in r.out and "起動しました" not in r.out


def test_main_exits_with_an_error_code_when_the_server_never_starts(launch, free_port, monkeypatch):
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)  # started は False のまま
    assert launch(default_port=free_port).code == 3


@pytest.mark.parametrize("how", ["flag", "env"])
def test_main_keeps_an_explicitly_chosen_port(launch, free_port, how):
    r = launch("--port", str(free_port), "--open") if how == "flag" else launch("--open", env_port=free_port)
    assert r.code == 0
    assert r.ports == [free_port]
    assert r.opened == [f"http://127.0.0.1:{free_port}"]


@pytest.mark.parametrize("how", ["flag", "env"])
def test_main_refuses_to_start_when_an_explicitly_chosen_port_is_busy(launch, busy_port, how):
    r = launch("--port", str(busy_port), "--open") if how == "flag" else launch("--open", env_port=busy_port)
    assert r.code == 1
    assert r.ports == [] and r.opened == []  # 勝手に別の番号へは移らない
    assert f"ポート {busy_port} " in r.out and "使用中" in r.out
    assert "起動しました" not in r.out


def test_main_flag_wins_over_the_environment_variable(launch, busy_port, free_port):
    r = launch("--port", str(free_port), env_port=busy_port)
    assert r.code == 0 and r.ports == [free_port]


def test_main_reports_a_bad_environment_variable_without_a_traceback(launch):
    r = launch(env_port="abc")
    assert r.code == 1
    assert "KEYCHANGER_PORT" in r.out and "abc" in r.out
    assert r.ports == []


def test_main_reports_an_unusable_host_without_a_traceback(launch, monkeypatch):
    monkeypatch.setattr(appmod, "ALLOWED_HOSTS", set(appmod.ALLOWED_HOSTS))
    r = launch("--host", "192.0.2.1")  # この PC のものではないアドレス(文書用の予約アドレス)
    assert r.code == 1
    assert "192.0.2.1" in r.out and "起動しました" not in r.out
    assert r.ports == []


def test_wrong_host_message_points_at_the_port_actually_in_use(launch, busy_port):
    r = launch(default_port=busy_port)
    (port,) = r.ports
    with TestClient(appmod.app, base_url="http://evil.example") as c:
        res = c.get("/api/status")
    assert res.status_code == 400
    assert f"http://127.0.0.1:{port} を開いてください" in res.json()["detail"]


# ---------------------------------------------------------------- 実際に起動する
def test_real_server_comes_up_on_another_port_when_8000_is_taken(tmp_path):
    """ユーザーの状況そのもの: 8000 番がふさがっているところへ、何も指定せずに起動する。"""
    try:
        blocker = listen_on(8000)
    except OSError:
        blocker = None  # すでに何かが 8000 番を使っている。それならそのまま試せる
    env = {k: v for k, v in os.environ.items() if not k.startswith("KEYCHANGER_")}
    env.update(KEYCHANGER_CACHE=str(tmp_path / "cache"), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], env=env, cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8")
    try:
        lines, url = [], None
        for line in proc.stdout:
            lines.append(line)
            m = re.search(r"ブラウザで (http://127\.0\.0\.1:\d+) を開いてください", line)
            if m:
                url = m.group(1)
                break
        assert url, "".join(lines)
        assert url != "http://127.0.0.1:8000"
        assert any("ポート 8000 はほかのアプリが使用中" in line for line in lines), "".join(lines)
        body = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(url + "/api/status", timeout=2) as res:
                    body = res.read().decode()
                break
            except OSError:
                assert proc.poll() is None, proc.stdout.read()
                time.sleep(0.1)
        assert body and '"max_minutes"' in body
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if blocker:
            blocker.close()


# ---------------------------------------------------------------- Windows 用バッチファイル
BAT_FILES = sorted(ROOT.glob("*.bat"))


def test_there_are_batch_files_to_check():
    assert {p.name for p in BAT_FILES} >= {"start.bat", "update.bat"}


@pytest.mark.parametrize("bat", BAT_FILES, ids=lambda p: p.name)
def test_batch_file_is_plain_ascii(bat):
    # 日本語(UTF-8)と「chcp 65001」が同じバッチファイルにあると、cmd.exe はファイルの読む位置を見失い、
    # 後ろの行の途中(python の「hon」)をコマンドとして実行してしまう。英数字だけなら位置はずれない。
    data = bat.read_bytes()
    bad = sorted({b for b in data if b > 0x7E or (b < 0x20 and b not in (0x0D, 0x0A, 0x09))})
    assert not bad, f"{bat.name} に ASCII 以外のバイトがあります: {[hex(b) for b in bad]}"


@pytest.mark.parametrize("bat", BAT_FILES, ids=lambda p: p.name)
def test_batch_file_does_not_switch_the_code_page(bat):
    assert not re.search(rb"(?im)^\s*@?\s*chcp\b", bat.read_bytes())


@pytest.mark.parametrize("bat", BAT_FILES, ids=lambda p: p.name)
def test_batch_file_uses_windows_line_endings(bat):
    data = bat.read_bytes()
    assert data.endswith(b"\r\n")
    assert data.count(b"\n") == data.count(b"\r\n") and data.count(b"\r") == data.count(b"\r\n")


def test_start_bat_still_starts_the_launcher_and_waits_for_a_key():
    text = (ROOT / "start.bat").read_text("ascii")
    assert "py -3 launcher.py %*" in text and "python launcher.py %*" in text
    assert re.search(r"(?m)^:end\r?$", text) and text.rstrip().endswith("pause")


def test_japanese_help_for_a_missing_python_lives_outside_the_batch_file():
    text = (ROOT / "start.bat").read_text("ascii")
    assert r"docs\python-not-found.txt" in text
    help_text = (ROOT / "docs" / "python-not-found.txt").read_text("utf-8")
    assert "Python が見つかりません" in help_text
