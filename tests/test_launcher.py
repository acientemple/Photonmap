import socket
import threading
import urllib.request
from pathlib import Path

import pytest

import photonmap.launcher as launcher
from photonmap.launcher import LauncherServer, find_free_port


def test_prefers_requested_port_when_free():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert find_free_port(preferred=port) == port


def test_falls_back_when_preferred_port_busy():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        result = find_free_port(preferred=taken)
    assert result != taken
    assert result > 0


def _start(root, tmp_path, port=None):
    server = LauncherServer(root, port=port, workdir=tmp_path / "work", poll_seconds=0.5)
    server.start()
    return server


def test_serves_shell_page(photo_dir, tmp_path):
    server = _start(photo_dir, tmp_path)
    try:
        with urllib.request.urlopen(server.url) as res:
            assert res.status == 200
            assert "照片地图" in res.read().decode("utf-8")
    finally:
        server.stop()


def test_chinese_and_space_path(photo_dir, tmp_path):
    target = tmp_path / "我的 照片"
    target.mkdir()
    for item in Path(photo_dir).iterdir():
        if item.is_file():
            (target / item.name).write_bytes(item.read_bytes())
    server = _start(target, tmp_path)
    try:
        with urllib.request.urlopen(server.url) as res:
            assert res.status == 200
    finally:
        server.stop()


def test_stop_releases_port(photo_dir, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = _start(photo_dir, tmp_path, port=port)
    server.stop()
    with socket.socket() as again:
        again.bind(("127.0.0.1", port))


def test_stop_twice_is_safe(photo_dir, tmp_path):
    server = _start(photo_dir, tmp_path)
    server.stop()
    server.stop()


def test_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        LauncherServer(tmp_path / "nope")


def test_main_cancelled_returns_zero():
    assert launcher.main([], dialog=lambda: "") == 0


def test_main_reports_missing_dir(monkeypatch):
    seen = []
    monkeypatch.setattr(launcher, "_error_box", seen.append)
    result = launcher.main(["PhotonMap.exe", "Z:/definitely/not/here"])
    assert result == 1
    assert seen and "目录" in seen[0]


def test_main_runs_gui(photo_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "run_gui", lambda path: calls.append(path))
    assert launcher.main(["PhotonMap.exe", str(photo_dir)]) == 0
    assert calls == [Path(photo_dir)]


def test_boot_reports_ready_server(photo_dir, tmp_path):
    state = {}
    done = threading.Event()
    launcher._boot(photo_dir, None, state, done.set, workdir=tmp_path / "work")
    assert done.wait(30)
    assert state["error"] is None
    server = state["server"]
    try:
        with urllib.request.urlopen(server.url) as res:
            assert res.status == 200
    finally:
        server.stop()


def test_boot_reports_error(tmp_path):
    state = {}
    done = threading.Event()
    launcher._boot(tmp_path / "nope", None, state, done.set, workdir=tmp_path / "work")
    assert done.wait(10)
    assert isinstance(state["error"], FileNotFoundError)
    assert state["server"] is None


def test_log_exception_writes_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    try:
        raise ValueError("boom")
    except ValueError:
        launcher._log_exception()
    log = tmp_path / "photonmap" / "launcher.log"
    assert "ValueError: boom" in log.read_text(encoding="utf-8")


def test_falls_back_to_next_port_before_random():
    """8765 被占用时优先用 8766，而不是随机端口——地址才稳定。"""
    base = 18765
    busy = socket.socket()
    try:
        busy.bind(("127.0.0.1", base))
    except OSError:
        busy.close()
        pytest.skip("测试端口被占用")
    try:
        busy.listen(1)
        assert find_free_port(preferred=base) == base + 1
    finally:
        busy.close()


def test_setup_logging_writes_server_log(tmp_path, monkeypatch):
    import logging

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    logger = logging.getLogger()
    # 测试进程里可能已经装过 handler，先摘掉再加
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    monkeypatch.setattr(logger, "_photonmap_file", False, raising=False)

    launcher._setup_logging()
    logging.getLogger("photonmap.server").error("boom")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "photonmap" / "server.log").read_text(encoding="utf-8")
    assert "boom" in text

    launcher._setup_logging()  # 重复调用不能重复加 handler
    assert sum(1 for h in logger.handlers if getattr(h, "baseFilename", "").endswith("server.log")) == 1


def test_should_stop_when_page_says_bye():
    # 页面关闭信号 + 一段时间没有新请求 → 退出
    assert launcher._should_stop(now=100.0, ready_at=0.0, last_activity=94.0, bye_at=94.0)
    # 刚收到关停信号（宽限期内）不退出，给刷新页面留机会
    assert not launcher._should_stop(now=100.0, ready_at=0.0, last_activity=97.0, bye_at=97.0)
    # 关停信号之后又有请求（页面刷新了）→ 不退出
    assert not launcher._should_stop(now=100.0, ready_at=0.0, last_activity=99.0, bye_at=90.0)


def test_should_stop_when_browser_never_connected():
    assert not launcher._should_stop(now=60.0, ready_at=0.0, last_activity=0.0, bye_at=None)
    assert launcher._should_stop(now=200.0, ready_at=0.0, last_activity=0.0, bye_at=None)


def test_should_stop_after_long_idle():
    assert not launcher._should_stop(now=500.0, ready_at=0.0, last_activity=10.0, bye_at=None)
    assert launcher._should_stop(now=1000.0, ready_at=0.0, last_activity=10.0, bye_at=None)
