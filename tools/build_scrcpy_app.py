# -*- coding: utf-8 -*-
"""把 scrcpy 官方程序打进控制台，产出一个双击即用的桌面应用 exe。

前置：
    pip install pyinstaller pywebview
（pywebview 在 Windows 上会自动带上 pythonnet / clr_loader / bottle）

用法：
    python build_scrcpy_app.py                       # 自动下载官方 scrcpy 并构建
    python build_scrcpy_app.py --scrcpy-dir D:\\scrcpy\\scrcpy-win64-v4.1
    python build_scrcpy_app.py --version 4.1 --out .\\dist

产出：
    <out>/scrcpy控制台.exe    桌面应用：原生窗口，内置 scrcpy + adb，无需 Python/浏览器
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"

DEFAULT_VERSION = "4.1"
RELEASE_URL = ("https://github.com/Genymobile/scrcpy/releases/download/"
               "v{ver}/scrcpy-win64-v{ver}.zip")

# 需要一起打进 exe 的 scrcpy 运行文件
BIN_FILES = [
    "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libusb-1.0.dll",
    "scrcpy.exe", "scrcpy-server", "SDL3.dll",
    "avcodec-62.dll", "avformat-62.dll", "avutil-60.dll", "swresample-6.dll",
    "LICENSE.txt", "scrcpy.png", "disconnected.png",
]

# pywebview 依赖链，全部 collect 保证打包后能拉起 WebView2
COLLECT = ["webview", "pythonnet", "clr_loader", "bottle", "proxy_tools"]


def download_scrcpy(version, workdir):
    """从 GitHub Releases 下载官方 scrcpy win64 包并解压。"""
    url = RELEASE_URL.format(ver=version)
    zip_path = workdir / f"scrcpy-win64-v{version}.zip"
    print("下载:", url)
    req = urllib.request.Request(url, headers={"User-Agent": "scrcpy-console-builder"})
    with urllib.request.urlopen(req, timeout=120) as r, open(zip_path, "wb") as f:
        f.write(r.read())
    print("解压:", zip_path, f"{zip_path.stat().st_size/1024/1024:.1f} MB")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(workdir)
    # 官方 zip 内是 scrcpy-win64-vX.Y/scrcpy-win64-vX.Y/ 两层
    for d in workdir.rglob("scrcpy.exe"):
        return d.parent
    raise SystemExit("解压后未找到 scrcpy.exe")


def locate_bin_dir(args, workdir):
    if args.scrcpy_dir:
        d = Path(args.scrcpy_dir)
        if not (d / "scrcpy.exe").exists():
            raise SystemExit(f"{d} 下没有 scrcpy.exe")
        return d
    return download_scrcpy(args.version, workdir)


def build(bin_dir, out_dir):
    if not (SRC / "app.py").exists():
        raise SystemExit(f"找不到源码目录: {SRC}")

    work = out_dir / "_build"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    missing = [f for f in BIN_FILES if not (bin_dir / f).exists()]
    if missing:
        raise SystemExit("缺少文件: " + ", ".join(missing))

    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--windowed", "--clean", "--noconfirm",
           "--icon", str(SRC / "app.ico"),
           "--name", "scrcpy控制台",
           "--distpath", str(out_dir),
           "--workpath", str(work / "w"),
           "--specpath", str(work)]
    for f in BIN_FILES:
        cmd += ["--add-data", f"{bin_dir / f};scrcpy-bin"]
    cmd += ["--add-data", f"{SRC / 'ui.html'};.",
            "--add-data", f"{SRC / 'app.ico'};."]
    for pkg in COLLECT:
        cmd += ["--collect-all", pkg]
    cmd.append(str(SRC / "app.py"))

    print("构建中（含 .NET 运行链，约 1-3 分钟）…")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = "\n".join((r.stdout or "").splitlines()[-8:])
    print(tail)
    if r.returncode != 0:
        print((r.stderr or "")[-2500:])
        raise SystemExit("PyInstaller 构建失败")

    exe = out_dir / "scrcpy控制台.exe"
    shutil.copy2(bin_dir / "LICENSE.txt", out_dir / "LICENSE-scrcpy.txt")
    shutil.rmtree(work, ignore_errors=True)
    return exe


def main():
    ap = argparse.ArgumentParser(description="构建 scrcpy 控制台桌面版")
    ap.add_argument("--scrcpy-dir", help="本地 scrcpy 目录（含 scrcpy.exe），不给则自动下载")
    ap.add_argument("--version", default=DEFAULT_VERSION, help="scrcpy 版本，默认 4.1")
    ap.add_argument("--out", default=str(HERE / "dist"), help="输出目录")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "_tmp"
    workdir.mkdir(exist_ok=True)

    bin_dir = locate_bin_dir(args, workdir)
    print("scrcpy 目录:", bin_dir)

    exe = build(bin_dir, out_dir)
    shutil.rmtree(workdir, ignore_errors=True)
    print("\n完成:", exe, f"{exe.stat().st_size/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
