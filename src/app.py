# -*- coding: utf-8 -*-
"""
scrcpy 控制台 —— 桌面应用入口
内嵌本地 HTTP 服务 + WebView2 原生窗口，不再打开浏览器。
WebView2 不可用时回退到 Edge App 模式，再不行才回退默认浏览器。
"""
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path


def resource(name):
    """定位随包资源：开发态在脚本目录，打包后在 _MEIPASS。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / name
        if p.exists():
            return p
    return Path(__file__).resolve().parent / name


def start_backend():
    """把 server.py 的 HTTP 服务跑在本进程的后台线程里。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import server as backend

    port = backend.pick_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), backend.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return backend, srv, port


def edge_app_window(url, width, height):
    """回退方案：用 Edge 的 --app 模式开一个无地址栏的独立窗口。"""
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(exe).exists():
            profile = Path.home() / "AppData" / "Local" / "scrcpy-console-edge"
            subprocess.Popen([exe, "--app=" + url,
                              "--window-size=%d,%d" % (width, height),
                              "--user-data-dir=" + str(profile)])
            return True
    return False


def main():
    width, height = 1280, 880
    try:
        backend, srv, port = start_backend()
    except Exception as e:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "本地服务启动失败：\n%s" % e, "scrcpy 控制台", 0x10)
        return 1

    url = "http://127.0.0.1:%d/" % port
    icon = resource("app.ico")
    storage = Path.home() / "AppData" / "Local" / "scrcpy-console" / "webview"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    try:
        import webview
        webview.create_window(
            "scrcpy 控制台", url,
            width=width, height=height, min_size=(1000, 660),
            background_color="#F4F4F5", text_select=True,
        )
        webview.start(
            icon=str(icon) if icon.exists() else None,
            private_mode=False,
            storage_path=str(storage),
        )
    except Exception as e:
        # WebView2 缺失或创建失败 → Edge App 模式 → 最后才是浏览器
        if not edge_app_window(url, width, height):
            import webbrowser
            webbrowser.open(url)
        try:
            print("native window unavailable (%s), fallback applied." % e)
            input()
        except (EOFError, OSError):
            pass

    srv.shutdown()
    backend.RUNNER.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
