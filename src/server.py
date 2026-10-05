# -*- coding: utf-8 -*-
"""
scrcpy 图形控制台 —— 本地后端
纯 Python 标准库实现，无需 pip 安装任何依赖。
启动后自动打开浏览器访问 http://127.0.0.1:<port>
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 打包成 exe 时 __file__ 不可靠，改用 exe 所在目录
BASE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)          # .../scrcpy-gui

# 单文件 exe 会把内置的 scrcpy 释放到临时目录 sys._MEIPASS
BUNDLE_DIR = Path(sys._MEIPASS) if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else None


def _find_scrcpy_dir():
    """定位 scrcpy.exe 所在目录：优先用外部真实目录（方便自行升级），其次用内置包。"""
    cands = [BASE.parent, BASE]
    if BUNDLE_DIR:
        cands.append(BUNDLE_DIR / "scrcpy-bin")
        cands.append(BUNDLE_DIR)
    for d in cands:
        if (d / "scrcpy.exe").exists():
            return d
    return BASE.parent


SCRCPY_DIR = _find_scrcpy_dir()
SCRCPY = SCRCPY_DIR / "scrcpy.exe"
# adb 可能只在内置包里（外部目录缺 adb 时回退）
ADB = SCRCPY_DIR / "adb.exe"
if not ADB.exists() and BUNDLE_DIR:
    for d in (BUNDLE_DIR / "scrcpy-bin", BUNDLE_DIR):
        if (d / "adb.exe").exists():
            ADB = d / "adb.exe"
            break

def _ui_file():
    """页面文件：优先用 exe 旁边的（方便改界面），没有就用内置的那份。"""
    p = BASE / "ui.html"
    if p.exists():
        return p
    if BUNDLE_DIR and (BUNDLE_DIR / "ui.html").exists():
        return BUNDLE_DIR / "ui.html"
    return p


UI_FILE = _ui_file()

SETTINGS_FILE = BASE / "settings.json"
PRESETS_FILE = BASE / "presets.json"
LOG_DIR = BASE / "logs"
SHOT_DIR = BASE / "screenshots"
UPLOAD_DIR = BASE / "uploads"
VIDEO_DIR = BASE / "videos"
LOG_FILE = LOG_DIR / "scrcpy.log"
CMD_LOG = LOG_DIR / "console.log"

for d in (LOG_DIR, SHOT_DIR, UPLOAD_DIR, VIDEO_DIR):
    d.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def si_hidden():
    s = subprocess.STARTUPINFO()
    s.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    s.wShowWindow = 0
    return s


# ------------------------------------------------- Windows Job Object 兜底
# 把本程序启动的所有子进程（scrcpy / adb server）放进一个 Job，并设置
# KILL_ON_JOB_CLOSE：主进程无论以何种方式退出（正常关窗、崩溃、被杀），
# 内核都会自动终止 Job 里的全部子进程并释放对 _MEI 临时目录的文件锁，
# PyInstaller 退出时就能正常清理临时目录，不再弹
# "Failed to remove temporary directory" 警告。
JOB_HANDLE = None


def _create_kill_on_close_job():
    import ctypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info),                     # JobObjectExtendedLimitInformation
            ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def _assign_to_job(job, proc):
    if not job:
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        k32.AssignProcessToJobObject(job, int(proc._handle))
    except Exception:
        pass


JOB_HANDLE = _create_kill_on_close_job()


# ---------------------------------------------------------------- 进程管理
class Runner:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.last_cmd = ""

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args):
        with self.lock:
            if self.running:
                return False, "scrcpy 已在运行中"
            if not SCRCPY.exists():
                return False, "找不到 scrcpy.exe：%s" % SCRCPY
            try:
                LOG_FILE.write_text("", encoding="utf-8")
            except Exception:
                pass
            cmdline = '"%s" %s' % (SCRCPY, " ".join(args))
            self.last_cmd = cmdline
            append_console("$ " + cmdline)
            f = open(LOG_FILE, "ab")
            try:
                self.proc = subprocess.Popen(
                    [str(SCRCPY)] + args,
                    cwd=str(SCRCPY_DIR),
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    startupinfo=si_hidden(),
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
                _assign_to_job(JOB_HANDLE, self.proc)
            except Exception as e:
                f.close()
                return False, "启动失败：%s" % e
            pid = self.proc.pid
            threading.Thread(target=self._watch, args=(pid,), daemon=True).start()
            return True, "已启动（pid=%d）" % pid

    def _watch(self, pid):
        p = self.proc
        if p is None:
            return
        rc = p.wait()
        append_console("[scrcpy] 进程退出，返回码 %s" % rc)

    def stop(self):
        with self.lock:
            if not self.running:
                self.proc = None
                return True, "没有正在运行的 scrcpy"
            p = self.proc
            try:
                p.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            for _ in range(30):
                if p.poll() is not None:
                    break
                time.sleep(0.1)
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
                for _ in range(30):
                    if p.poll() is not None:
                        break
                    time.sleep(0.1)
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
            rc = p.poll()
            self.proc = None
            append_console("[scrcpy] 已停止（返回码 %s）" % rc)
            return True, "已停止"


RUNNER = Runner()


def append_console(text):
    try:
        with open(CMD_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), text))
    except Exception:
        pass


def read_tail(path, max_lines=300):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return ""
    try:
        txt = data.decode("utf-8", "replace")
    except Exception:
        txt = str(data)
    lines = txt.splitlines()
    return "\n".join(lines[-max_lines:])


# ---------------------------------------------------------------- adb 工具
def adb(args, serial=None, timeout=25):
    cmd = [str(ADB)]
    if serial:
        cmd += ["-s", serial]
    cmd += [str(a) for a in args]
    try:
        p = subprocess.Popen(
            cmd, cwd=str(SCRCPY_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            startupinfo=si_hidden(), creationflags=CREATE_NO_WINDOW,
        )
        _assign_to_job(JOB_HANDLE, p)       # adb fork 出的 server 同样进 Job
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            return 1, "timeout"
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err or "")
        return p.returncode, (out + err).strip()
    except Exception as e:
        return -1, "执行失败：%s" % e


def list_devices():
    rc, out = adb(["devices", "-l"])
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = {"serial": serial, "state": state, "model": "", "transport": ""}
        for p in parts[2:]:
            if p.startswith("model:"):
                info["model"] = p.split(":", 1)[1].replace("_", " ")
            elif p.startswith("transport_id:"):
                info["transport"] = p.split(":", 1)[1]
        devices.append(info)
    return devices


def device_ip(serial=None):
    for cmd in (["shell", "ip", "addr", "show", "wlan0"],
                ["shell", "ifconfig", "wlan0"]):
        rc, out = adb(cmd, serial)
        if rc == 0 and out:
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
    return ""


# ---------------------------------------------------------------- 命令构建
def _v(s, k, default=""):
    x = s.get(k, default)
    return "" if x is None else str(x).strip()


def build_args(s):
    a = []

    def add(*vals):
        for v in vals:
            a.append(str(v))

    # ---- 设备选择
    # scrcpy 只允许出现一个设备选择器，必须互斥，否则报
    # "At most one device selector option may be passed"。
    # 优先级：无线地址（且未指定设备） > --tcpip 自动 > -d/-e > 指定序列号
    serial = _v(s, "serial")
    sel = _v(s, "selectMode", "auto")
    tcpip_mode = _v(s, "tcpipMode", "off")
    addr = _v(s, "tcpipAddr")
    has_serial = bool(serial) and serial != "auto"
    if tcpip_mode == "addr" and addr and not has_serial:
        add("--tcpip=" + addr)
    elif tcpip_mode == "auto" and not has_serial:
        add("--tcpip")
    elif sel == "usb":
        add("-d")
    elif sel == "tcpip":
        add("-e")
    elif has_serial:
        add("--serial", serial)

    # ---- 视频
    if not s.get("video", True):
        add("--no-video")
    if _v(s, "maxSize"):
        add("-m", _v(s, "maxSize"))
    if _v(s, "videoBitRate"):
        add("-b", _v(s, "videoBitRate"))
    if _v(s, "maxFps"):
        add("--max-fps", _v(s, "maxFps"))
    codec = _v(s, "videoCodec", "default")
    if codec and codec != "default":
        add("--video-codec", codec)
    if _v(s, "videoEncoder"):
        add("--video-encoder", _v(s, "videoEncoder"))
    if _v(s, "videoBuffer"):
        add("--video-buffer", _v(s, "videoBuffer"))
    if _v(s, "crop"):
        add("--crop", _v(s, "crop"))
    co = _v(s, "captureOrientation", "0")
    if co and co != "0":
        add("--capture-orientation", co)
    if s.get("printFps"):
        add("--print-fps")
    if s.get("noDownsizeOnError"):
        add("--no-downsize-on-error")
    if s.get("ignoreEncoderConstraints"):
        add("--ignore-video-encoder-constraints")
    if _v(s, "minSizeAlignment") and _v(s, "minSizeAlignment") != "1":
        add("--min-size-alignment", _v(s, "minSizeAlignment"))

    # ---- 音频
    if not s.get("audio", True):
        add("--no-audio")
    else:
        src = _v(s, "audioSource", "output")
        if src and src != "output":
            add("--audio-source", src)
        if _v(s, "audioBitRate") and _v(s, "audioBitRate") != "128K":
            add("--audio-bit-rate", _v(s, "audioBitRate"))
        if _v(s, "audioBuffer") and _v(s, "audioBuffer") != "50":
            add("--audio-buffer", _v(s, "audioBuffer"))
        ac = _v(s, "audioCodec", "default")
        if ac and ac != "default":
            add("--audio-codec", ac)
        if s.get("audioDup"):
            add("--audio-dup")
        if s.get("requireAudio"):
            add("--require-audio")
        if not s.get("audioPlayback", True):
            add("--no-audio-playback")

    # ---- 窗口
    if _v(s, "windowWidth"):
        add("--window-width", _v(s, "windowWidth"))
    if _v(s, "windowHeight"):
        add("--window-height", _v(s, "windowHeight"))
    if _v(s, "windowX"):
        add("--window-x", _v(s, "windowX"))
    if _v(s, "windowY"):
        add("--window-y", _v(s, "windowY"))
    if s.get("borderless"):
        add("--window-borderless")
    if s.get("alwaysOnTop"):
        add("--always-on-top")
    if s.get("fullscreen"):
        add("-f")
    if s.get("disableScreensaver"):
        add("--disable-screensaver")
    if _v(s, "windowTitle"):
        add("--window-title", _v(s, "windowTitle"))
    rd = _v(s, "renderDriver", "default")
    if rd and rd != "default":
        add("--render-driver", rd)
    rf = _v(s, "renderFit", "default")
    if rf and rf != "default":
        add("--render-fit", rf)
    if _v(s, "backgroundColor"):
        add("--background-color", _v(s, "backgroundColor"))
    if s.get("noAspectRatioLock"):
        add("--no-window-aspect-ratio-lock")
    if s.get("noMipmaps"):
        add("--no-mipmaps")
    if not s.get("videoPlayback", True):
        add("--no-video-playback")
    if s.get("noWindow"):
        add("--no-window")

    # ---- 控制
    if not s.get("control", True):
        add("-n")
    mm = _v(s, "mouseMode", "default")
    if mm and mm != "default":
        add("--mouse=" + mm)
    km = _v(s, "keyboardMode", "default")
    if km and km != "default":
        add("--keyboard=" + km)
    gm = _v(s, "gamepadMode", "default")
    if gm and gm != "default":
        add("--gamepad=" + gm)
    if s.get("hidKeyboard") and km == "default":
        add("-K")
    if s.get("hidMouse") and mm == "default":
        add("-M")
    if s.get("hidGamepad") and gm == "default":
        add("-G")
    if s.get("showTouches"):
        add("-t")
    if s.get("stayAwake"):
        add("-w")
    if s.get("turnScreenOff"):
        add("-S")
    if s.get("powerOffOnClose"):
        add("--power-off-on-close")
    if s.get("keepActive"):
        add("--keep-active")
    if not s.get("clipboardSync", True):
        add("--no-clipboard-autosync")
    if s.get("legacyPaste"):
        add("--legacy-paste")
    if s.get("preferText"):
        add("--prefer-text")
    if s.get("otg"):
        add("--otg")
    if s.get("noKeyRepeat"):
        add("--no-key-repeat")
    if s.get("noMouseHover"):
        add("--no-mouse-hover")
    if not s.get("powerOn", True):
        add("--no-power-on")
    sm = _v(s, "shortcutMod", "default")
    if sm and sm != "default":
        add("--shortcut-mod=" + sm)
    if _v(s, "pushTarget") and _v(s, "pushTarget") != "/sdcard/Download/":
        add("--push-target", _v(s, "pushTarget"))
    if _v(s, "screenOffTimeout"):
        add("--screen-off-timeout", _v(s, "screenOffTimeout"))

    # ---- 录制
    if s.get("record"):
        path = _v(s, "recordFile")
        if path:
            add("-r", path)
            fmt = _v(s, "recordFormat", "auto")
            if fmt and fmt != "auto":
                add("--record-format", fmt)
            ro = _v(s, "recordOrientation", "0")
            if ro and ro != "0":
                add("--record-orientation", ro)

    # ---- 摄像头
    vsrc = _v(s, "videoSource", "display")
    if vsrc == "camera":
        add("--video-source=camera")
        if _v(s, "cameraId"):
            add("--camera-id", _v(s, "cameraId"))
        if _v(s, "cameraSize"):
            add("--camera-size", _v(s, "cameraSize"))
        cf = _v(s, "cameraFacing", "default")
        if cf and cf != "default":
            add("--camera-facing=" + cf)
        if _v(s, "cameraAr"):
            add("--camera-ar", _v(s, "cameraAr"))
        if _v(s, "cameraFps"):
            add("--camera-fps", _v(s, "cameraFps"))
        if s.get("cameraTorch"):
            add("--camera-torch")
        if s.get("cameraHighSpeed"):
            add("--camera-high-speed")
        if _v(s, "cameraZoom"):
            add("--camera-zoom", _v(s, "cameraZoom"))

    # ---- 显示 / 虚拟显示
    if _v(s, "displayId") and _v(s, "displayId") != "0":
        add("--display-id", _v(s, "displayId"))
    do = _v(s, "displayOrientation", "0")
    if do and do != "0":
        add("--display-orientation", do)
    dip = _v(s, "displayImePolicy", "default")
    if dip and dip != "default":
        add("--display-ime-policy=" + dip)
    if s.get("newDisplay"):
        w = _v(s, "newDisplaySize")
        dpi = _v(s, "newDisplayDpi")
        if w and dpi:
            add("--new-display=%s/%s" % (w, dpi))
        elif w:
            add("--new-display=" + w)
        elif dpi:
            add("--new-display=/" + dpi)
        else:
            add("--new-display")
    if _v(s, "startApp"):
        app = _v(s, "startApp")
        if s.get("forceStopApp"):
            app = "+" + app
        if s.get("fuzzyApp"):
            app = "?" + app if not app.startswith("+") else app[:1] + "?" + app[1:]
        add("--start-app=" + app)
    if s.get("vdNoDecorations"):
        add("--no-vd-system-decorations")
    if s.get("vdKeepContent"):
        add("--no-vd-destroy-content")
    if s.get("flexDisplay"):
        add("-x")

    # ---- 高级
    vb = _v(s, "verbosity", "info")
    if vb and vb != "info":
        add("-V", vb)
    if _v(s, "port") and _v(s, "port") != "27183:27199":
        add("-p", _v(s, "port"))
    if _v(s, "timeLimit"):
        add("--time-limit", _v(s, "timeLimit"))
    if s.get("forceAdbForward"):
        add("--force-adb-forward")
    if _v(s, "tunnelHost"):
        add("--tunnel-host", _v(s, "tunnelHost"))
    if _v(s, "tunnelPort"):
        add("--tunnel-port", _v(s, "tunnelPort"))
    if s.get("killAdbOnClose"):
        add("--kill-adb-on-close")
    if s.get("noCleanup"):
        add("--no-cleanup")
    if s.get("pauseOnExit"):
        add("--pause-on-exit=if-error")
    if s.get("pauseOnExitAlways"):
        add("--pause-on-exit=true")

    return a


def cmd_string(args):
    def q(x):
        if x == "" or re.search(r"[\s\"]", x):
            return '"%s"' % x
        return x
    return "scrcpy.exe " + " ".join(q(x) for x in args)


# ---------------------------------------------------------------- HTTP
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def do_adb_action(data):
    act = data.get("action", "")
    serial = data.get("serial") or None
    if serial == "auto":
        serial = None
    param = data.get("param", "")

    if act == "devices":
        return {"ok": True, "devices": list_devices()}

    if act == "tcpip":
        rc, out = adb(["tcpip", param or "5555"], serial)
        ip = device_ip(serial)
        return {"ok": rc == 0, "msg": out or ("已切换到 TCP/IP 模式，设备 IP：%s" % ip if ip else "已切换到 TCP/IP 模式"), "ip": ip}

    if act == "ip":
        ip = device_ip(serial)
        return {"ok": bool(ip), "msg": ip or "未能获取设备 IP（需连接 Wi-Fi）", "ip": ip}

    if act == "pair":
        # Android 11+ 无线调试：免插线首次配对（adb pair <ip:port> <code>）
        addr = param.strip()
        code = str(data.get("code", "")).strip()
        if not addr or not code:
            return {"ok": False, "msg": "请填写配对地址和 6 位配对码"}
        rc, out = adb(["pair", addr, code], None, timeout=60)
        low = (out or "").lower()
        ok = rc == 0 and ("successfully" in low or "paired" in low)
        if not ok and ("already paired" in low or "already" in low):
            ok = True
        return {"ok": ok, "msg": out or ("配对成功" if ok else "配对失败")}

    if act == "connect":
        rc, out = adb(["connect", param])
        return {"ok": rc == 0 and "connected" in out.lower(), "msg": out}

    if act == "disconnect":
        rc, out = adb(["disconnect"] if not param else ["disconnect", param])
        return {"ok": True, "msg": out or "已断开"}

    if act == "usb":
        rc, out = adb(["usb"], serial)
        return {"ok": rc == 0, "msg": out or "已切回 USB 模式"}

    if act == "restart-adb":
        adb(["kill-server"])
        rc, out = adb(["start-server"])
        return {"ok": True, "msg": "adb 服务已重启"}

    if act == "keyevent":
        rc, out = adb(["shell", "input", "keyevent", param], serial)
        return {"ok": rc == 0, "msg": out or "keyevent %s 已发送" % param}

    if act == "text":
        rc, out = adb(["shell", "input", "text", param.replace(" ", "%s")], serial)
        return {"ok": rc == 0, "msg": out or "文本已输入"}

    if act == "url":
        rc, out = adb(["shell", "am", "start", "-a", "android.intent.action.VIEW",
                       "-d", param], serial)
        return {"ok": rc == 0, "msg": out or "已打开：%s" % param}

    if act == "app":
        rc, out = adb(["shell", "monkey", "-p", param,
                       "-c", "android.intent.category.LAUNCHER", "1"], serial)
        return {"ok": rc == 0, "msg": out or "已启动 %s" % param}

    if act == "screenshot":
        name = "screen_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SHOT_DIR / name
        cmd = [str(ADB)]
        if serial:
            cmd += ["-s", serial]
        cmd += ["exec-out", "screencap", "-p"]
        try:
            with open(path, "wb") as f:
                cp = subprocess.Popen(cmd, cwd=str(SCRCPY_DIR), stdout=f,
                                      stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                      startupinfo=si_hidden(),
                                      creationflags=CREATE_NO_WINDOW)
                _assign_to_job(JOB_HANDLE, cp)
                try:
                    _, cerr = cp.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    cp.kill()
                    cp.communicate()
            ok = path.exists() and path.stat().st_size > 0
            return {"ok": ok, "msg": str(path) if ok else "截图失败", "path": str(path)}
        except Exception as e:
            return {"ok": False, "msg": "截图失败：%s" % e}

    if act == "install":
        p = Path(param)
        if not p.exists():
            return {"ok": False, "msg": "文件不存在：%s" % param}
        rc, out = adb(["install", "-r", str(p)], serial, timeout=180)
        return {"ok": rc == 0, "msg": out}

    if act == "push":
        p = Path(param)
        if not p.exists():
            return {"ok": False, "msg": "文件不存在：%s" % param}
        rc, out = adb(["push", str(p), data.get("dest") or "/sdcard/Download/"],
                      serial, timeout=180)
        return {"ok": rc == 0, "msg": out}

    if act == "shell":
        rc, out = adb(["shell"] + param.split(), serial)
        return {"ok": rc == 0, "msg": out}

    if act == "scrcpy-list":
        what = param  # cameras / displays / encoders / apps
        rc, out = adb([], serial)  # noop, 保证 adb 在线
        cmd = [str(SCRCPY), "--list-" + what]
        if serial:
            cmd += ["-s", serial]
        try:
            p = subprocess.run(cmd, cwd=str(SCRCPY_DIR), capture_output=True,
                               timeout=60, startupinfo=si_hidden(),
                               creationflags=CREATE_NO_WINDOW)
            txt = (p.stdout or b"").decode("utf-8", "replace") + \
                  (p.stderr or b"").decode("utf-8", "replace")
            return {"ok": p.returncode == 0, "msg": txt.strip()}
        except Exception as e:
            return {"ok": False, "msg": "执行失败：%s" % e}

    return {"ok": False, "msg": "未知操作：%s" % act}


class Handler(BaseHTTPRequestHandler):
    server_version = "scrcpy-gui/1.0"

    def log_message(self, fmt, *args):
        pass

    def _headers(self, ctype, length=None):
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._headers("application/json; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, ctype):
        try:
            data = path.read_bytes()
        except Exception:
            self.send_error(404)
            return
        self.send_response(200)
        self._headers(ctype, len(data))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/index.html", "/ui.html"):
            self._file(UI_FILE, "text/html; charset=utf-8")
            return

        if p == "/api/info":
            self._json({
                "scrcpyDir": str(SCRCPY_DIR),
                "scrcpy": str(SCRCPY),
                "adb": str(ADB),
                "hasScrcpy": SCRCPY.exists(),
                "hasAdb": ADB.exists(),
                "running": RUNNER.running,
                "shotDir": str(SHOT_DIR),
                "videoDir": str(VIDEO_DIR),
            })
            return

        if p == "/api/devices":
            self._json({"ok": True, "devices": list_devices()})
            return

        if p == "/api/settings":
            self._json(load_json(SETTINGS_FILE, {}))
            return

        if p == "/api/presets":
            self._json(load_json(PRESETS_FILE, {}))
            return

        if p == "/api/status":
            self._json({
                "running": RUNNER.running,
                "cmd": RUNNER.last_cmd,
                "log": read_tail(LOG_FILE, 200),
                "console": read_tail(CMD_LOG, 200),
            })
            return

        if p == "/api/log":
            self._json({"log": read_tail(LOG_FILE, 400)})
            return

        self.send_error(404)

    # ---------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p == "/api/build":
            s = json.loads(self._body().decode("utf-8") or "{}")
            args = build_args(s)
            self._json({"ok": True, "args": args, "cmd": cmd_string(args)})
            return

        if p == "/api/start":
            s = json.loads(self._body().decode("utf-8") or "{}")
            rf = str(s.get("recordFile") or "").strip()
            if rf:
                try:
                    Path(rf).parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
            args = build_args(s)
            ok, msg = RUNNER.start(args)
            self._json({"ok": ok, "msg": msg, "cmd": cmd_string(args)})
            return

        if p == "/api/stop":
            ok, msg = RUNNER.stop()
            self._json({"ok": ok, "msg": msg})
            return

        if p == "/api/settings":
            data = json.loads(self._body().decode("utf-8") or "{}")
            save_json(SETTINGS_FILE, data)
            self._json({"ok": True})
            return

        if p == "/api/presets/save":
            data = json.loads(self._body().decode("utf-8") or "{}")
            presets = load_json(PRESETS_FILE, {})
            presets[data.get("name", "预设")] = data.get("settings", {})
            save_json(PRESETS_FILE, presets)
            self._json({"ok": True, "presets": presets})
            return

        if p == "/api/presets/delete":
            data = json.loads(self._body().decode("utf-8") or "{}")
            presets = load_json(PRESETS_FILE, {})
            presets.pop(data.get("name", ""), None)
            save_json(PRESETS_FILE, presets)
            self._json({"ok": True, "presets": presets})
            return

        if p == "/api/adb":
            data = json.loads(self._body().decode("utf-8") or "{}")
            try:
                res = do_adb_action(data)
            except Exception as e:
                res = {"ok": False, "msg": "异常：%s" % e}
            if data.get("action") not in ("devices",):
                append_console("adb %s -> %s" % (data.get("action"),
                                                 str(res.get("msg"))[:300]))
            self._json(res)
            return

        if p == "/api/upload":
            name = (q.get("name") or ["upload.bin"])[0]
            name = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", name)
            dest = UPLOAD_DIR / ("%s_%s" % (datetime.now().strftime("%H%M%S"), name))
            dest.write_bytes(self._body())
            self._json({"ok": True, "path": str(dest)})
            return

        if p == "/api/open":
            data = json.loads(self._body().decode("utf-8") or "{}")
            path = data.get("path", "")
            try:
                if os.name == "nt":
                    os.startfile(path)  # noqa
                else:
                    subprocess.Popen(["xdg-open", path])
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)})
            return

        self.send_error(404)


def pick_port(start=8770):
    import socket
    for port in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main():
    port = pick_port()
    url = "http://127.0.0.1:%d/" % port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 56)
    print("  scrcpy 图形控制台已启动")
    print("  scrcpy 目录：%s" % SCRCPY_DIR)
    print("  访问地址：%s" % url)
    print("  关闭此窗口即可退出控制台")
    print("=" * 56)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.stop()


if __name__ == "__main__":
    main()
