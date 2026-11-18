"""双击启动器：选照片目录 → 起本地服务 → 打开浏览器。

这个模块只在 GUI 里用；命令行仍然走 cli.py。
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .cli import DEFAULT_TILE
from .server import Watcher, build_server

_LOG_NAME = "launcher.log"

# 页面关闭后的宽限期（刷新页面会立刻重新连上，不能一收到就退）
BYE_GRACE = 5.0
# 启动后多久还没等到浏览器连上就放弃（浏览器没打开成功时别留下隐形常驻进程）
CONNECT_TIMEOUT = 120.0
# 长时间没有任何请求（比如浏览器崩溃没发关停信号）就自动退出
IDLE_TIMEOUT = 900.0


def _should_stop(
    now: float,
    ready_at: float,
    last_activity: float,
    bye_at: float | None,
) -> bool:
    """启动器是否该退出：页面关了、一直没连上，或长时间没有请求。"""
    if bye_at is not None and now - bye_at > BYE_GRACE and now - last_activity > BYE_GRACE:
        return True
    if last_activity <= ready_at and now - ready_at > CONNECT_TIMEOUT:
        return True
    return now - last_activity > IDLE_TIMEOUT


def find_free_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    """优先用 preferred；被占用就试 preferred+1…+9，最后才让系统随便给一个。

    不直接用系统随机端口，是为了让地址稳定（127.0.0.1:8766 这种比
    59257 好记，也不至于每次启动都变）。
    """
    for port in [preferred, *range(preferred + 1, preferred + 10)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _log_dir() -> Path:
    """日志目录，跟派生资源一样放在用户缓存下。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "photonmap"


def _write_log(lines: list[str]) -> None:
    """无控制台的 exe 里，异常只能落到文件。"""
    try:
        path = _log_dir() / _LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
            handle.writelines(lines)
            handle.write("\n")
    except Exception:  # pragma: no cover - 日志写不进去也不能再炸
        pass


def _setup_logging() -> None:
    """把服务端日志落到文件里。

    打包后的 exe 没有控制台，`logging` 默认写到 stderr（为 None），
    请求里抛的异常（比如某张图解码失败）就彻底看不见了。
    """
    root_logger = logging.getLogger()
    if getattr(root_logger, "_photonmap_file", False):
        return
    root_logger._photonmap_file = True
    root_logger.setLevel(logging.INFO)
    try:
        path = _log_dir() / "server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root_logger.addHandler(handler)


def _config_path() -> Path:
    return _log_dir() / "launcher.json"


def _load_saved_folders() -> list[Path]:
    """上次打开过的照片文件夹（都还存在才用）。"""
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, list):
        return []
    result: list[Path] = []
    for item in folders:
        if not isinstance(item, str):
            continue
        path = Path(item)
        if path.is_dir() and path not in result:
            result.append(path)
    return result


def _save_folders(folders) -> None:
    payload = {"folders": [str(Path(item)) for item in folders]}
    try:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _log_exception() -> None:
    """把当前异常（traceback）追加到日志。"""
    _write_log(traceback.format_exc().splitlines(keepends=True))


class LauncherServer:
    """包一层 build_server + Watcher，给出可启动、可停止、可重复停止的服务。"""

    def __init__(
        self,
        root: Path | str | list,
        host: str = "127.0.0.1",
        port: int | None = None,
        workdir: Path | None = None,
        poll_seconds: float = 2.0,
        progress=None,
    ) -> None:
        roots = [Path(item) for item in (root if isinstance(root, (list, tuple)) else [root])]
        missing = [item for item in roots if not item.exists()]
        roots = [item for item in roots if item.exists()]
        if not roots:
            raise FileNotFoundError(f"照片目录不存在: {missing[0] if missing else root}")
        self._poll_seconds = max(0.5, poll_seconds)
        self._port = find_free_port(host, 8765) if port is None else port
        self.server = build_server(
            roots,
            tiles=[dict(DEFAULT_TILE)],
            workdir=Path(workdir) if workdir else None,
            host=host,
            port=self._port,
            poll_ms=int(self._poll_seconds * 1000),
            progress=progress,
        )
        self._watcher: Watcher | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        if self._started:
            return
        self._watcher = Watcher(self.server.library, self._poll_seconds)
        self._watcher.start()
        self.server.watcher = self._watcher
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        watcher = getattr(self.server, "watcher", self._watcher)
        if watcher is not None:
            watcher.stop()
        self._watcher = None
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _boot(
    root_path: Path,
    progress,
    state: dict,
    on_done,
    workdir: Path | None = None,
) -> threading.Thread:
    """后台线程里建服务；结果写进 state，最后调 on_done()（可能在工作线程里）。"""

    def worker() -> None:
        try:
            server = LauncherServer(root_path, workdir=workdir, progress=progress)
            server.start()
        except Exception as exc:
            state["error"] = exc
            state["server"] = None
        else:
            state["error"] = None
            state["server"] = server
        finally:
            on_done()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def _ask_directory() -> str:
    """弹文件夹选择框；取消返回空串。"""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="选择照片目录", parent=root)
    finally:
        root.destroy()


def _error_box(message: str) -> None:
    """出错时弹一个消息框（打包成无控制台窗口后没有别的地方可看）。"""
    messagebox.showerror("PhotonMap", message)


def _enable_dpi_awareness() -> None:
    """Windows 高分屏下别让窗口糊掉。要在建任何 Tk 窗口之前调用。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # pragma: no cover - 非 Windows 或旧系统
        pass


def run_gui(root_path, initial_open: bool = True) -> None:
    """先出窗口，再在后台扫描目录；关窗即停止服务。"""
    roots = [Path(item) for item in (root_path if isinstance(root_path, (list, tuple)) else [root_path])]
    root = tk.Tk()
    root.title("PhotonMap 照片地图")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    status_var = tk.StringVar(value="正在扫描照片…")
    url_var = tk.StringVar(value="")
    progress = {"done": 0, "total": 0}
    state: dict = {"server": None, "error": None}
    stopped = {"value": False}

    ttk.Label(frame, text="照片目录").grid(row=0, column=0, sticky="w")
    ttk.Label(frame, text="；".join(str(item) for item in roots), wraplength=380).grid(
        row=0, column=1, sticky="w", padx=(8, 0)
    )
    ttk.Label(frame, text="状态").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Label(frame, textvariable=status_var).grid(
        row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
    )
    ttk.Label(frame, text="访问地址").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(frame, textvariable=url_var, state="readonly", width=42).grid(
        row=2, column=1, sticky="we", padx=(8, 0), pady=(8, 0)
    )

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(16, 0))

    def open_page() -> None:
        url = url_var.get()
        if url:
            webbrowser.open(url)

    def stop_app() -> None:
        if stopped["value"]:
            return
        stopped["value"] = True
        server = state.get("server")
        if server is not None:
            try:
                server.stop()
            except Exception:
                _log_exception()
        root.destroy()

    def on_callback_error(exc_type, exc_value, exc_tb) -> None:
        _write_log(traceback.format_exception(exc_type, exc_value, exc_tb))
        _error_box(f"操作失败：{exc_value}")

    ttk.Button(buttons, text="打开网页", command=open_page).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="停止", command=stop_app).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", stop_app)
    root.report_callback_exception = on_callback_error

    def report(done: int, total: int) -> None:
        progress["done"] = done
        progress["total"] = total

    def poll() -> None:
        if stopped["value"]:
            return
        error = state.get("error")
        if error is not None:
            _error_box(f"启动失败：{error}")
            stop_app()
            return
        server = state.get("server")
        if server is not None:
            url_var.set(server.url)
            status_var.set("服务已启动")
            if initial_open:
                root.after(300, open_page)
            # 加载完成就把窗口收起来，只在扫描/出错时出现
            saved = {"roots": [str(item) for item in server.server.library.roots]}
            _save_folders(server.server.library.roots)
            root.after(300, root.withdraw)
            root.after(1000, monitor, server, time.monotonic(), saved)
            return
        if progress["total"]:
            status_var.set(
                f"正在扫描照片 {progress['done']}/{progress['total']}…"
                "（首次运行要生成缩略图，照片多时需要较久）"
            )
        root.after(200, poll)

    def monitor(launcher_server, ready_at: float, saved: dict) -> None:
        """盯住网页是否还活着：网页关了就把服务和进程一起收掉。"""
        if stopped["value"]:
            return
        live = launcher_server.server
        roots_now = [str(item) for item in getattr(live.library, "roots", [])]
        if roots_now and roots_now != saved["roots"]:
            saved["roots"] = roots_now
            _save_folders(live.library.roots)
        now = time.monotonic()
        last = getattr(live, "last_activity", ready_at)
        bye_at = getattr(live, "bye_at", None)
        if _should_stop(now, ready_at, last, bye_at):
            stop_app()
            return
        root.after(1000, monitor, launcher_server, ready_at, saved)

    _boot(roots, report, state, lambda: None)
    root.after(200, poll)
    root.mainloop()


def _run(argv: list[str] | None = None, dialog=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    dialog = dialog or _ask_directory
    folders: list[Path] = []
    if len(argv) > 1 and argv[1].strip():
        folders = [Path(argv[1].strip())]
    else:
        folders = _load_saved_folders()
        if not folders:
            choice = dialog()
            if not choice:
                return 0
            folders = [Path(choice)]

    valid = [item for item in folders if item.is_dir()]
    if not valid:
        _error_box(f"照片目录不存在：{folders[0]}")
        return 1
    run_gui(valid)
    return 0


def main(argv: list[str] | None = None, dialog=None) -> int:
    """入口：给了目录参数就用它，否则弹选择框。"""
    _setup_logging()
    _enable_dpi_awareness()
    try:
        return _run(argv, dialog)
    except Exception as exc:
        _log_exception()
        _error_box(f"启动器出错：{exc}\n日志：{_log_dir() / _LOG_NAME}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
