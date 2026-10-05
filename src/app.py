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


def _si_hidden():
    s = subprocess.STARTUPINFO()
    s.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    s.wShowWindow = 0
    return s


def resource(name):
    """定位随包资源：开发态在脚本目录，打包后在 _MEIPASS。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / name
        if p.exists():
            return p
    return Path(__file__).resolve().parent / name


def cleanup_children(backend):
    """退出前收干净由本程序启动的子进程。

    adb server 是常驻进程，从 PyInstaller 的 _MEI 临时目录里运行并锁住
    adb.exe 的映像文件；不杀掉它，PyInstaller 退出清理 _MEI 目录时会
    弹 "Failed to remove temporary directory" 警告。
    """
    # 1. 停掉正在运行的 scrcpy
    try:
        backend.RUNNER.stop()
    except Exception:
        pass
    # 2. 优雅关闭 adb server
    try:
        adb = Path(backend.ADB)
        if adb.exists():
            subprocess.run(
                [str(adb), "kill-server"], timeout=8,
                capture_output=True, startupinfo=_si_hidden(),
                creationflags=0x08000000,
            )
    except Exception:
        pass
    # 3. 兜底：强制结束仍在 _MEI 临时目录里运行的进程（只匹配本实例
    #    的临时目录，不影响系统里其他来源的 adb，比如模拟器自带的）
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        safe = str(mei).replace("'", "''")
        ps = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.ExecutablePath -like '%s\\*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
              % safe)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=15, capture_output=True,
                startupinfo=_si_hidden(), creationflags=0x08000000,
            )
        except Exception:
            pass


def cleanup_stale_mei():
    """启动时顺手清理上次异常退出残留的 _MEI* 空壳目录（可失败，静默）。"""
    if not getattr(sys, "frozen", False):
        return
    tmp = Path.home() / "AppData" / "Local" / "Temp"
    meipass = getattr(sys, "_MEIPASS", "")
    try:
        for d in tmp.glob("_MEI*"):
            if d.is_dir() and str(d) != meipass:
                subprocess.run(
                    ["cmd", "/c", "rd", "/s", "/q", str(d)],
                    timeout=10, capture_output=True,
                    startupinfo=_si_hidden(), creationflags=0x08000000,
                )
    except Exception:
        pass


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
    cleanup_stale_mei()

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
    cleanup_children(backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
