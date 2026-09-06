"""Apple Music 桌面歌词 - 入口。

启动后常驻系统托盘：
  - 后台线程通过 Windows SMTC 监听 Apple Music 的播放状态
  - 新歌曲自动到 LRCLib / 网易云抓取带时间轴的歌词
  - 屏幕底部悬浮歌词条逐行高亮，可拖动、可调透明度/字号

跨线程通信全部通过 Qt 信号（会自动排队到主线程执行），
不要在监控/歌词线程里直接操作任何界面对象。
"""

import sys
import threading

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication

from lyrics_fetcher import LyricsFetcher
from lyrics_window import LyricsWindow
from media_monitor import MediaMonitor

# 高 DPI 支持（必须在 QApplication 创建之前设置）
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

APP_NAME = "Apple Music 桌面歌词"


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # 关掉悬浮窗不退出，只留托盘
    app.setStyle("Fusion")

    # 中文字体兜底
    if QFont("Microsoft YaHei").exactMatch():
        app.setFont(QFont("Microsoft YaHei", 9))

    fetcher = LyricsFetcher()
    window = LyricsWindow(fetcher)
    window.show()

    # 监控线程只会调 win.media_changed.emit(...)，
    # 该信号已连接 window._on_media 并自动以 QueuedConnection 投递到主线程
    monitor = MediaMonitor(
        on_update=lambda info: window.media_changed.emit(info),
        poll_interval=0.5,
    )
    threading.Thread(target=monitor.start, name="smtc-monitor", daemon=True).start()

    code = app.exec_()
    sys.exit(code)


if __name__ == "__main__":
    main()
