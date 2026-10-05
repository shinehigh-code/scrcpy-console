# 第三方组件声明

本项目是对 [scrcpy](https://github.com/Genymobile/scrcpy) 的**图形化封装**，并非替代实现。
核心的屏幕采集、视频编码、设备端控制能力完全由 scrcpy 提供。

---

## 1. scrcpy（核心依赖）

| 项目 | 说明 |
|---|---|
| 名称 | scrcpy |
| 版本 | 4.1（win64 官方发布包） |
| 主页 | https://github.com/Genymobile/scrcpy |
| 许可证 | **Apache License 2.0** |
| 版权 | Copyright (C) 2018 Genymobile；Copyright (C) 2018-2026 Romain Vimont |

内置/使用的文件（来自官方 `scrcpy-win64-v4.1.zip`）：

```
scrcpy.exe          客户端主程序
scrcpy-server       推送到 Android 设备上运行的服务端
adb.exe             设备调试桥（来自 Android Platform Tools）
AdbWinApi.dll / AdbWinUsbApi.dll / libusb-1.0.dll   adb 依赖
SDL3.dll            窗口与输入
avcodec-62.dll / avformat-62.dll / avutil-60.dll / swresample-6.dll   音视频编解码
scrcpy.png / disconnected.png   窗口图标
```

这些组件的原始许可证以 scrcpy 官方发布包为准；adb 部分遵循
Android SDK 的 Apache-2.0 许可，SDL、FFmpeg 等分别遵循各自上游许可证。
完整的 Apache-2.0 文本见仓库根目录 `LICENSE`。

## 2. pywebview（桌面窗口）

| 项目 | 说明 |
|---|---|
| 名称 | pywebview |
| 主页 | https://github.com/r0x0r/pywebview |
| 许可证 | BSD 3-Clause |
| 用途 | 提供原生桌面窗口（Windows 上基于 WebView2 / Edge Chromium） |

运行时依赖：`pythonnet`、`clr_loader`、`bottle`、`proxy_tools`。

## 3. 本项目自身代码

`src/app.py`、`src/server.py`、`src/ui.html`、`src/app.ico`、`tools/`
由本项目编写，同样以 **Apache License 2.0** 发布（见 `LICENSE`）。

---

## 分发说明

若你重新分发本项目的打包产物，请一并保留：

1. 本文件（`THIRD-PARTY-NOTICES.md`）
2. `LICENSE`（Apache-2.0 全文）
3. scrcpy 官方发布包中的 `LICENSE.txt`

并在显著位置注明项目基于 Genymobile/scrcpy 构建。
