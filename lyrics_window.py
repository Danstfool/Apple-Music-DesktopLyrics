"""Apple Music 桌面歌词（PyQt5，自绘，第三版）。

设计要点：
- 逐字卡拉OK：LRC 只有“行”级时间，按本行到下行的时长线性插值出进度，
  在当前句内部从左到右逐字变色（唱到哪亮到哪）。
- 没有“歌词条/面板”档位断层：窗口多高就显示多少行（行高=字号固定），
  1 行时退化为单行滚动显示当前句。
- 所有文字只在绘制时逐字画一次：同色同权重，避免抗锯齿叠边产生的锯齿。
- 任务栏吸附：可拖动、右下角可调宽度；防止系统最小化/失活导致消失。
"""

import ctypes
import os
import sys
import threading
import time
from bisect import bisect_right

from PyQt5.QtCore import QEvent, QRect, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

from lyrics_fetcher import LyricLine, LyricsFetcher
from media_monitor import MediaInfo

DEBUG = os.environ.get("DESKLYR_DEBUG") == "1"

FONT_FAMILY = "Microsoft YaHei"

# ---------- 布局 ----------
MIN_W = 260
MIN_H = 40
PAD_X = 14            # 左右留白
AUTO_PANEL_H = 270    # “恢复自动尺寸”时的默认高度

# 字号(px，只随菜单档位变化) & 行高
FONT_PX = {0: 17, 1: 21, 2: 25}
ROW_H = {0: 27, 1: 33, 2: 39}

GROW_CAP = 2400       # 长句“不换行”时允许的最大宽度
WRAP_CAP = 820        # 长句“自动换行”时面板最大宽度
AUTO_W_LO = 420

# 颜色
COL_BG = (16, 17, 22)
COL_SUNG = QColor("#ffffff")         # 已唱到的字（当前行）
COL_REST = QColor("#d3d7dd")         # 当前行尚未唱到的字
COL_FUTURE = QColor("#a8aeb8")       # 还没唱的行
COL_PAST = QColor("#767b85")         # 唱过的行
COL_MUTED = QColor("#757b85")
COL_SUNG_PAUSED = QColor("#9aa0aa")

STATUS_WAIT = "等待 Apple Music 播放…"

SCROLL_SPEED = 60.0   # 单行模式横向滚动速度 px/s
SCROLL_LEAD = 0.7     # 每句开始滚动前的停顿(秒)
SCROLL_END_HOLD = 1.0  # 滚到末尾后的停留(秒)，避免瞬间跳回起点

ADVANCE_OPTIONS = [
    (-300, "延迟 300ms"),
    (0, "正常 (0ms)"),
    (150, "提前 150ms"),
    (300, "提前 300ms"),
    (400, "提前 400ms (推荐)"),
    (500, "提前 500ms"),
    (650, "提前 650ms"),
    (800, "提前 800ms"),
]

# 切句时的列表滑动过渡
SLIDE_DURATION = 0.26   # 秒

ROW_PRESETS = [1, 2, 3, 5, 8]  # 行数预设


class LyricsWindow(QWidget):
    media_changed = pyqtSignal(object)
    _lyrics_ready = pyqtSignal(str, object, str)  # (key, lines|None, label)

    # ==================================================================
    def __init__(self, fetcher: LyricsFetcher):
        super().__init__(None)
        self._settings = QSettings("DesktopLyrics", "AppleLyrics")
        self._fetcher = fetcher

        self._notice = ""          # 临时浮动提示（换歌词反馈等）
        self._notice_until = 0.0
        # ---- 播放状态 ----
        self._media: MediaInfo | None = None
        self._media_key = ""
        self._lines: list[LyricLine] = []
        self._times: list[int] = []
        self._index = -1
        self._fetch_key = ""
        self._fetch_ckey = ""
        self._fetch_timer = 0.0
        self._fetch_empty = False
        self._base_pos_ms = 0
        self._base_at = 0.0
        self._paused = False

        # ---- 显示 ----
        self._cur_txt = ""
        self._seg_cache = {}
        self._seg_widths = {}
        self._seg_key = ("", "")     # (窗口宽, 字号px)
        self._scroll_x = 0.0
        self._scroll_phase = "lead"  # lead / run / end
        self._scroll_timer = SCROLL_LEAD
        self._slide_px = 0.0        # 切句列表滑动过渡（像素，衰减到 0）
        self._slide_speed = 0.0
        self._last_clock = time.time()

        # ---- 设置 ----
        self._opacity = float(self._settings.value("opacity", 0.92))
        self._scale = int(self._settings.value("scale", 1))
        self._always_on_top = self._settings.value("ontop", "true") != "false"
        self._click_through = self._settings.value("clickthrough", "false") == "true"
        self._long_mode = self._settings.value("longmode", "wrap")   # wrap|grow
        adv = self._settings.value("advance", 400, type=int)
        self._advance_ms = adv if any(adv == v for v, _ in ADVANCE_OPTIONS) else 400
        self._docked = self._settings.value("docked", "false") == "true"
        self._locked = self._settings.value("locked", "false") == "true"
        self._karaoke = self._settings.value("karaoke", "true") == "true"

        # ---- 几何 ----
        self._manual = self._settings.value("manual", "false") == "true"
        self._win_w = int(self._settings.value("w", 0, type=int))
        self._win_h = int(self._settings.value("h", 0, type=int))
        self._use_center = self._settings.value("usecenter", "true") == "true"
        self._drag = None
        self._resize = None
        self._hidden_intentional = False
        self._disp_pos_ms = -1      # 单调的显示进度（防止高亮回退）
        self._rep_val = -1          # Apple 最近一次上报值（防领先钳制用）
        self._rep_changed_at = 0.0  # 该上报值“出现”的时间
        self._last_force_top = 0.0  # 周期性强制置顶的时间戳
        self._hook = None           # 全局鼠标钩子句柄
        self._hook_cb = None
        self._dock_x = 0
        self._dock_y = 0
        self._dock_w = 0
        self._dock_h = 0

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_W, MIN_H)

        self._build_tray()
        self._apply_flags()

        self._restore_geometry()
        self.media_changed.connect(self._on_media, Qt.QueuedConnection)
        self._lyrics_ready.connect(self._on_lyrics, Qt.QueuedConnection)
        self._refresh_text(force=True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)   # ~30fps，滚动/扫光更顺滑

    # ==================================================================
    # 托盘 / 菜单
    # ==================================================================
    def _icon_paths(self):
        """自定义托盘图标的查找位置（exe 同目录 icon.ico 优先）。"""
        candidates = []
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(sys.executable)
            candidates.append(os.path.join(exe_dir, "icon.ico"))
        else:
            candidates.append(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "icon.ico"))
        try:
            candidates.append(os.path.join(QApplication.applicationDirPath(),
                                           "icon.ico"))
        except Exception:
            pass
        return [c for c in candidates if os.path.isfile(c)]

    def _make_icon(self) -> QIcon:
        # 优先使用 exe 同目录的自定义 icon.ico（换图无需重新打包）
        for path in self._icon_paths():
            try:
                ic = QIcon(path)
                if not ic.isNull():
                    return ic
            except Exception:
                pass
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(90, 120, 255))
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setPen(QColor("#ffffff"))
        f = QFont("Segoe UI", 30, QFont.Bold)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "♪")
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self._make_icon())
        self._tray.setToolTip("Apple Music 桌面歌词")
        # 每次右键都现生成菜单，保证勾选状态实时正确
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _popup_menu(self):
        from PyQt5.QtGui import QCursor
        menu = QMenu()
        self._fill_menu(menu)
        self._popup_keep = menu
        menu.popup(QCursor.pos())

    def _fill_menu(self, menu):
        def act(text, fn):
            a = QAction(text, self)
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        act("显示 / 隐藏歌词", self._toggle_visible)
        menu.addSeparator()

        m_rows = menu.addMenu("固定显示行数")
        for n in ROW_PRESETS:
            a = QAction(f"{n} 行", self)
            a.triggered.connect(lambda checked=False, x=n: self._preset_rows(x))
            m_rows.addAction(a)

        menu.addSeparator()

        a = QAction("窗口置顶", self)
        a.setCheckable(True)
        a.setChecked(self._always_on_top)
        a.toggled.connect(self._toggle_ontop)
        menu.addAction(a)

        b = QAction("鼠标穿透（推荐开启，任务栏可正常点击）", self)
        b.setCheckable(True)
        b.setChecked(self._click_through)
        b.toggled.connect(self._toggle_click_through)
        menu.addAction(b)

        c = QAction("吸附到任务栏显示（自动开启鼠标穿透）", self)
        c.setCheckable(True)
        c.setChecked(self._docked)
        c.toggled.connect(self._toggle_dock)
        menu.addAction(c)

        if self._docked:
            m_pos = menu.addMenu("任务栏内位置")
            for key, label in [("left", "靠左"), ("center", "居中"), ("right", "靠右")]:
                a = QAction(label, self)
                a.triggered.connect(lambda checked=False, k=key: self._dock_align(k))
                m_pos.addAction(a)

        d = QAction("锁定常显（禁止隐藏/消失，解锁后才能隐藏）", self)
        d.setCheckable(True)
        d.setChecked(self._locked)
        d.toggled.connect(self._toggle_lock)
        menu.addAction(d)

        k = QAction("KTV 逐句扫光效果", self)
        k.setCheckable(True)
        k.setChecked(self._karaoke)
        k.toggled.connect(self._set_karaoke)
        menu.addAction(k)

        menu.addSeparator()

        m_long = menu.addMenu("长句处理")
        g_long = QActionGroup(self)
        g_long.setExclusive(True)
        for mode, label in [("wrap", "自动换行 (推荐)"), ("grow", "不换行，窗口变宽")]:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setChecked(self._long_mode == mode)
            g_long.addAction(a)
            a.triggered.connect(lambda checked=False, m=mode: self._set_long_mode(m))
            m_long.addAction(a)

        m_sync = menu.addMenu("歌词同步提前量")
        g_sync = QActionGroup(self)
        g_sync.setExclusive(True)
        for ms, label in ADVANCE_OPTIONS:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setChecked(self._advance_ms == ms)
            g_sync.addAction(a)
            a.triggered.connect(lambda checked=False, v=ms: self._set_advance(v))
            m_sync.addAction(a)

        m_op = menu.addMenu("透明度")
        for v, name in [(0.5, "50%"), (0.65, "65%"), (0.8, "80%"), (0.92, "92%"), (1.0, "100%")]:
            a = QAction(name, self)
            a.triggered.connect(lambda checked=False, x=v: self._set_opacity(x))
            m_op.addAction(a)

        m_size = menu.addMenu("字号")
        for k, name in enumerate(["小", "中", "大"]):
            a = QAction(name, self)
            a.triggered.connect(lambda checked=False, x=k: self._set_scale(x))
            m_size.addAction(a)

        menu.addSeparator()
        act("恢复自动尺寸", self._restore_auto_size)
        act("换下一份歌词 " + self._candidate_menu_hint(), self._cycle_lyrics)
        act("重新获取当前歌词", self._refetch_current)
        menu.addSeparator()
        act("退出", self._quit)

    def _candidate_menu_hint(self) -> str:
        """菜单项上直接显示候选状态，避免“黑盒”。"""
        m = self._media
        if m is None or not m.title:
            return "（无播放）"
        try:
            key = self._fetcher.key(m.title, m.artist, m.duration_ms)
            info = self._fetcher.candidate_info(key)
        except Exception:
            info = None
        if info is None:
            return "（候选未就绪）"
        idx, total, label = info
        if total <= 1:
            return "（仅此一份）"
        return f"（{idx}/{total}：{label}）"

    # ==================================================================
    # 动作
    # ==================================================================
    def _toggle_visible(self):
        if self._locked:
            # 锁定状态不允许隐藏（防止误操作后歌词“消失”）
            if not self.isVisible():
                self.show()
            return
        if self.isVisible():
            self._hidden_intentional = True
            self.hide()
        else:
            self._hidden_intentional = False
            self.show()

    def _toggle_lock(self, on: bool):
        self._locked = on
        self._settings.setValue("locked", "true" if on else "false")
        if on and not self.isVisible():
            self._hidden_intentional = False
            self.show()
            self.raise_()

    def _toggle_ontop(self, on: bool):
        self._always_on_top = on
        self._settings.setValue("ontop", "true" if on else "false")
        self._restart_window()

    def _toggle_click_through(self, on: bool):
        self._click_through = on
        self._settings.setValue("clickthrough", "true" if on else "false")
        self._restart_window()

    def _restart_window(self):
        """改变窗口标志（置顶/穿透等）后 Windows 会销毁并重建原生窗口，
        若直接原地修改可能让窗口“消失”。稳妥流程：记住位置 -> 隐藏
        -> 改标志 -> 延迟一拍再显示。
        """
        if not self.isVisible():
            self._apply_flags()
            return
        geo = self.geometry()
        self._hidden_intentional = False
        self.hide()
        self._apply_flags()
        self.setGeometry(geo)
        QTimer.singleShot(0, self._show_after_restart)

    def _show_after_restart(self):
        if self._locked or not self._hidden_intentional:
            self.show()
            self.raise_()
            if self._docked:
                self.move(self.x(), self._dock_y)
                self._force_topmost()

    def _toggle_dock(self, on: bool):
        self._docked = on
        self._settings.setValue("docked", "true" if on else "false")
        if on:
            # 吸附任务栏时默认开启鼠标穿透：
            # 不拦截点击 -> 任务栏图标/系统按钮照常使用，歌词不会被“点没”
            if not self._click_through:
                self._click_through = True
                self._settings.setValue("clickthrough", "true")
            self._save_geometry()
            self._setup_dock()
            self._dock_resize_restore()
            self._install_click_hook()
        else:
            self._uninstall_click_hook()
            self._restore_geometry_from_saved()
        self._apply_flags()
        self._reshow()
        if self._docked and self.isVisible():
            self._force_topmost()
            self._last_force_top = time.time()

    def _dock_align(self, key: str):
        """任务栏内水平对齐（穿透模式下仍可调整位置）。"""
        if not self._docked:
            return
        sc = QApplication.primaryScreen()
        if sc is None:
            return
        full = sc.geometry()
        nw = self.width()
        if key == "left":
            x = full.left() + 4
        elif key == "right":
            x = full.right() - nw - 4
        else:
            x = full.center().x() - nw // 2
        self.move(max(2, x), self._dock_y)
        self._settings.setValue("pos", self.pos())

    def _preset_rows(self, n: int):
        """固定显示行数（把窗口拉成 n 行的高度）。"""
        if self._docked:
            return
        self._manual = True
        self._settings.setValue("manual", "true")
        row_h = ROW_H[self._scale]
        h = max(MIN_H, n * row_h + 4)
        w = max(self.width(), self._preferred_width())
        self.resize(w, h)
        self._settings.setValue("w", w)
        self._settings.setValue("h", h)
        self._clamp_into_screen()
        self._reset_draw_cache()
        self.update()

    def _set_karaoke(self, on: bool):
        self._karaoke = on
        self._settings.setValue("karaoke", "true" if on else "false")
        self.update()

    def _set_long_mode(self, mode: str):
        self._long_mode = mode
        self._settings.setValue("longmode", mode)
        if not self._manual:
            self._auto_size()
        self._reset_draw_cache()
        self.update()

    def _set_scale(self, k: int):
        if k not in (0, 1, 2):
            return
        self._scale = k
        self._settings.setValue("scale", k)
        self._reset_draw_cache()
        if not self._manual and not self._docked:
            self._auto_size()
        self.update()

    def _set_advance(self, ms: int):
        self._advance_ms = ms
        self._settings.setValue("advance", ms)

    def _set_opacity(self, v: float):
        self._opacity = v
        self._settings.setValue("opacity", v)
        self.update()

    def _restore_auto_size(self):
        if self._docked:
            return
        self._manual = False
        self._settings.setValue("manual", "false")
        self._auto_size()
        self.update()

    def _refetch_current(self):
        m = self._media
        if m is None or not m.title:
            return
        self._fetch_timer = 0
        self._fetch_empty = False
        self._fetcher.invalidate(m.title, m.artist, m.duration_ms)
        self._start_fetch(m)

    def _reshow(self):
        if self.isVisible():
            self.hide()
            self.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_visible()
        elif reason == QSystemTrayIcon.Context:
            self._popup_menu()

    def _quit(self):
        self._save_geometry()
        self._uninstall_click_hook()
        self._tray.hide()
        QApplication.instance().quit()

    # ==================================================================
    # 窗口标志 & 系统事件（防止点击别处后消失）
    # ==================================================================
    def _install_click_hook(self):
        """全局鼠标钩子：只要用户点鼠标（含任务栏/图标/桌面），
        立刻把歌词条压回最上层 —— 无论点击发生在置顶周期之间何时。"""
        try:
            if self._hook:
                return
            WH_MOUSE_LL = 14
            WM_LBUTTONDOWN = 0x0201
            WM_LBUTTONDBLCLK = 0x0203

            user32 = ctypes.windll.user32
            # windll 默认 32 位返回类型会截断 64 位句柄，显式声明
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            user32.UnhookWindowsHookEx.restype = ctypes.c_int
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]

            def proc(nCode, wParam, lParam):
                if nCode >= 0 and wParam in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK):
                    try:
                        QTimer.singleShot(0, self._on_global_click)
                    except Exception:
                        pass
                return user32.CallNextHookEx(
                    self._hook, nCode, wParam, lParam)

            # Python 3.13 的 ctypes.wintypes 已不含 LRESULT/WPARAM/LPARAM，
            # 直接用等价的整数类型
            self._hook_cb = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t,
                ctypes.c_void_p)(proc)
            # 低级鼠标钩子按“全局钩子”安装（dwThreadId=0）：
            # 回调会投递到安装线程的消息循环（即 Qt 主线程）执行
            tid = 0
            self._hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._hook_cb, None, tid)
            if not self._hook:
                self._hook_cb = None
                err = ctypes.windll.kernel32.GetLastError()
                print("[ui] 鼠标钩子安装失败, GetLastError =", err,
                      file=sys.stderr, flush=True)
        except Exception as e:
            print("[ui] 鼠标钩子安装异常:", repr(e), file=sys.stderr, flush=True)
            self._hook = None
            self._hook_cb = None

    def _uninstall_click_hook(self):
        try:
            if self._hook:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
        self._hook = None
        self._hook_cb = None

    def _on_global_click(self):
        if not self._docked or self._hidden_intentional:
            return
        if self._click_through:
            # 穿透模式：点击一律落到任务栏，点击后立刻把歌词压回最上层
            self._ensure_top_now()
            return
        # 非穿透模式：点在自己窗口上可能是拖动/缩放，跳过；点在外面视为点任务栏
        try:
            from PyQt5.QtGui import QCursor
            if not self.frameGeometry().contains(QCursor.pos()):
                self._ensure_top_now()
        except Exception:
            pass

    def _ensure_top_now(self):
        try:
            if self._hidden_intentional:
                return
            if not self.isVisible():
                self.show()
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self._force_topmost()
        except Exception:
            pass

    def _apply_flags(self):
        on_top = self._always_on_top or self._docked
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
        transparent = self._click_through
        self.setAttribute(Qt.WA_TransparentForMouseEvents, transparent)
        self.setWindowFlag(Qt.WindowTransparentForInput, transparent)

    def _force_topmost(self):
        """用 Win32 SetWindowPos(HWND_TOPMOST) 把自己压到任务栏之上。

        Qt 的 raise_() 只是 HWND_TOP，无法越过任务栏的置顶带；
        点击任务栏/任务栏图标后任务栏会盖住歌词条（看起来像消失），
        周期性地重新 TOPMOST 才能保证歌词条始终显示在任务栏上方。
        """
        try:
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception as e:
            print("[ui] 置顶失败:", repr(e), file=sys.stderr, flush=True)

    def changeEvent(self, event):
        # “显示桌面”等操作会最小化包括置顶在内的窗口：立刻恢复
        if event.type() == QEvent.WindowStateChange and self.isMinimized():
            if self._locked or self._docked or self._always_on_top:
                QTimer.singleShot(0, self._restore_from_minimize)
        super().changeEvent(event)

    def _restore_from_minimize(self):
        if not self._locked:
            self.showNormal()
        self.raise_()
        self.show()
        self._force_topmost()
        self._reset_marquee()

    def event(self, event):
        # 失活后（点别处）确保窗口仍在原位且在最上层（含穿透模式）
        if (event.type() in (QEvent.WindowDeactivate, QEvent.ApplicationDeactivate)
                and self.isVisible() and (self._locked or self._docked)):
            if self._docked:
                self._force_topmost()
            else:
                self.raise_()
        return super().event(event)

    # ==================================================================
    # 媒体入口
    # ==================================================================
    def _on_media(self, info):
        if info is None:
            self._media = None
            self._media_key = ""
            self._lines = []
            self._times = []
            self._index = -1
            self._fetch_empty = False
            self._refresh_text(force=True)
            return

        if not info.title and self._media is not None and self._media.title:
            info = MediaInfo(
                title=self._media.title,
                artist=self._media.artist,
                album=self._media.album,
                duration_ms=info.duration_ms or self._media.duration_ms,
                position_ms=info.position_ms,
                is_playing=info.is_playing,
                app_name=info.app_name,
            )

        key = info.song_key
        was_playing = bool(self._media and self._media.is_playing)
        is_playing = info.is_playing
        self._media = info
        self._paused = not is_playing

        if key != self._media_key:
            # ---------- 新歌 ----------
            self._media_key = key
            self._index = -1
            self._lines = []
            self._times = []
            self._fetch_empty = False
            self._base_pos_ms = info.position_ms
            self._base_at = time.time()
            self._disp_pos_ms = -1
            self._slide_px = 0.0
            self._slide_speed = 0.0
            self._track_report(info.position_ms, time.time())
            self._tray.setToolTip(f"Apple Music 桌面歌词\n{info.title} - {info.artist}")
            self._start_fetch(info)
            self._refresh_text(force=True)
            return

        # ---------- 同一首 ----------
        now = time.time()
        self._track_report(info.position_ms, now)
        if not is_playing:
            if was_playing:
                self._reset_marquee()
                self._slide_px = 0.0
                self._slide_speed = 0.0
            if abs(info.position_ms - self._base_pos_ms) > 1500:
                self._disp_pos_ms = -1   # 暂停期间拖动了进度条：允许重新定位
            self._base_pos_ms = info.position_ms
            self._base_at = now
        else:
            # 播放中的进度完全交给 _effective_pos 的“上报阶梯跟随”模型，
            # 这里只记录最新的整秒上报值（_track_report 已做）
            self._base_pos_ms = info.position_ms
            self._base_at = now
        if self._lines:
            self._tick_render()

    def _track_report(self, reported_ms: int, now: float):
        """记录 Apple 整秒上报值以及它“变化”的时刻。

        用于“不领先钳制”：正常时上限随整秒阶梯同步爬升（不产生
        每秒一次的停顿）；只有上报值超过 1.6s 没变（真正的缓冲/
        暂停滞后）才把上限按住。
        """
        if reported_ms != self._rep_val:
            self._rep_val = reported_ms
            self._rep_changed_at = now

    # ==================================================================
    # 歌词抓取
    # ==================================================================
    def _start_fetch(self, info: MediaInfo):
        key = info.song_key          # 原样歌曲标识（与 _media_key 一致）
        ckey = self._fetcher.key(info.title, info.artist,
                                 info.duration_ms)   # 清洗后候选池标识
        now = time.time()
        if self._fetch_key == key and now - self._fetch_timer < 60:
            return
        self._fetch_key = key
        self._fetch_ckey = ckey
        self._fetch_timer = now
        threading.Thread(target=self._fetch_worker, args=(key, info), daemon=True).start()

    def _fetch_worker(self, key, info: MediaInfo):
        def push(lines, label=""):
            try:
                self._lyrics_ready.emit(key, lines, label)
            except Exception:
                pass
        try:
            self._fetcher.fetch_streaming(
                info.title, info.artist, info.duration_ms, push)
        except Exception as e:
            print("[lyrics] 抓取异常:", repr(e), file=sys.stderr, flush=True)
            push(None)

    def _on_lyrics(self, key: str, lines, label: str = ""):
        if key != self._media_key:
            return
        if lines is None:
            lines = []
        if DEBUG:
            print(f"[ui] 歌词候选: {label} {len(lines)} 行", flush=True)
        self._set_lyrics(key, lines)

    def _set_lyrics(self, key: str, lines: list):
        self._fetch_empty = not bool(lines)
        self._lines = list(lines)
        self._times = [l.time_ms for l in self._lines]
        self._index = -1
        self._slide_px = 0.0
        self._slide_speed = 0.0
        if not self._manual and not self._docked:
            self._auto_size()     # 换歌才重新定宽
        if DEBUG:
            print(f"[ui] 歌词就绪 {len(self._lines)} 行", flush=True)
        self._refresh_text(force=True)

    def _cycle_lyrics(self):
        """换下一份候选歌词：带明确反馈（菜单状态/窗口提示/托盘/日志）。"""
        print("[cycle] 点击了“换下一份歌词”", flush=True)
        m = self._media
        if m is None or not m.title:
            self._flash_notice("当前没有播放歌曲")
            return
        key = self._fetcher.key(m.title, m.artist, m.duration_ms)
        res = self._fetcher.cycle_next(key)
        print(f"[cycle] key={key!r} fetch_key={self._fetch_key!r} "
              f"结果={None if res is None else (res['changed'], res['total'])}",
              flush=True)
        if res is None:
            # 候选还没就绪（抓取未完成）→ 主动重新抓
            self._flash_notice("歌词候选尚未就绪，正在重新搜索…")
            self._refetch_current()
            return
        if not res["changed"]:
            if res["total"] > 1 and res.get("reason") != "short_only":
                self._flash_notice(
                    f"共 {res['total']} 份候选但内容相同，无法通过切换改善；"
                    f"若仍不对请点「重新获取当前歌词」")
            else:
                self._flash_notice(
                    f"没有更完整的候选可切换（当前 {res['total']} 份）。"
                    f"仍不对的话，先在 Apple Music 打开歌词页一次，"
                    f"再点「重新获取」")
            return
        if key != self._fetch_ckey:
            print("[cycle] 歌曲key不一致，已忽略", flush=True)
            return
        self._set_lyrics(key, res["lines"])
        msg = f"已切换到歌词候选 {res['index'] + 1}/{res['total']}：{res['label']}"
        print("[cycle] " + msg, flush=True)
        self._flash_notice(msg)
        try:
            self._tray.setToolTip(f"Apple Music 桌面歌词\n{msg}")
            self._tray.showMessage("桌面歌词", msg, QSystemTrayIcon.Information,
                                   1800)
        except Exception:
            pass

    def _flash_notice(self, text: str, ms: int = 3200):
        self._notice = text
        self._notice_until = time.time() + ms / 1000.0
        self.update()

    # ==================================================================
    # 进度
    # ==================================================================
    def _effective_pos(self) -> int:
        """演唱进度（含提前量），单调不回退且不领先演唱。

        模型：Apple 整秒量化上报（每 ~1s 变一次），一旦“检测到”新值，
        就从 新值 + 自其出现起的墙钟时间 等速推进 —— 没有滞后积累、
        没有周期性停顿；上报值超过 1.5s 未变化（真正缓冲/卡顿）时
        才停住，避免歌词自己跑远。
        """
        if not self._media:
            return 0
        if not self._media.is_playing:
            return self._base_pos_ms + self._advance_ms
        now = time.time()
        if self._rep_val >= 0:
            since_ms = int((now - self._rep_changed_at) * 1000)
            pos = self._rep_val + min(since_ms, 1500)
        else:
            elapsed = int((now - self._base_at) * 1000)
            pos = self._base_pos_ms + elapsed
        if self._media.duration_ms > 0:
            pos = min(pos, self._media.duration_ms)
        if 0 <= self._disp_pos_ms < pos:
            self._disp_pos_ms = pos
        elif self._disp_pos_ms < 0:
            self._disp_pos_ms = pos
        else:
            pos = self._disp_pos_ms   # 禁止回退
        return pos + self._advance_ms

    def _line_progress(self) -> float:
        """当前行演唱进度 0..1（从本行时间线性插值到下一行时间）。"""
        if not self._lines or self._index < 0:
            return 0.0
        t0 = self._times[self._index]
        t1 = None
        for t in self._times[self._index + 1:]:
            if t > t0:
                t1 = t
                break
        if t1 is None:
            dur = self._media.duration_ms if self._media else 0
            t1 = dur if dur > t0 else t0 + 5000
        pos = self._effective_pos()
        # 扫光时长限幅：两句间隔超大时（间奏等）不跟着慢慢爬，
        # 超过 6s 一律按 6s 扫完（唱完即整句点亮，符合 KTV 观感）；
        # 间隔极短时也保证不至于闪跳。
        span = max(350, min(t1 - t0, 6000))
        f = (pos - t0) / span
        return max(0.0, min(f, 1.0))

    def _tick_render(self):
        if not self._media or not self._lines or not self._media.is_playing:
            return
        pos = self._effective_pos()
        idx = bisect_right(self._times, pos) - 1
        if idx < 0:
            idx = 0
        if idx >= len(self._lines):
            idx = len(self._lines) - 1
        if idx != self._index:
            old = self._index
            self._index = idx
            self._reset_marquee()
            self._start_slide(old)
        # 卡拉OK逐字进度与单行滚动都需要持续重绘
        self.update()

    def _start_slide(self, old_idx: int):
        """切句时让新的一句从下面一格平滑滑入，避免生硬跳切。"""
        self._slide_px = 0.0
        self._slide_speed = 0.0
        if old_idx < 0 or old_idx >= len(self._lines):
            return
        if self._docked or self._visible_rows() < 2:
            return
        self._slide_px = float(ROW_H[self._scale])
        self._slide_speed = ROW_H[self._scale] / SLIDE_DURATION

    def _tick(self):
        now = time.time()
        dt = now - self._last_clock
        if self._media and self._lines and self._media.is_playing:
            # 切句滑行动画推进
            if self._slide_px > 0:
                self._slide_px = max(0.0, self._slide_px - self._slide_speed * dt)
                if self._slide_px <= 0:
                    self._slide_speed = 0.0
            # 单行滚动（阶段机：句首停留 lead -> 平滑滚动 run -> 句尾停留 end -> 循环）
            if self._visible_rows() == 1:
                cur = self._cur_text_raw()
                if cur:
                    px = FONT_PX[self._scale]
                    fm = QFontMetrics(self._font(px, True))
                    max_w = self.width() - 2 * PAD_X
                    if fm.horizontalAdvance(cur) > max_w:
                        dt = now - self._last_clock
                        if self._scroll_phase == "lead":
                            self._scroll_timer -= dt
                            if self._scroll_timer <= 0:
                                self._scroll_phase = "run"
                        elif self._scroll_phase == "run":
                            limit = fm.horizontalAdvance(cur) + 40
                            if self._scroll_x > -limit:
                                self._scroll_x = max(
                                    -limit, self._scroll_x - SCROLL_SPEED * dt)
                            else:
                                self._scroll_phase = "end"
                                self._scroll_timer = SCROLL_END_HOLD
                        elif self._scroll_phase == "end":
                            self._scroll_timer -= dt
                            if self._scroll_timer <= 0:
                                self._scroll_x = 0.0
                                self._scroll_phase = "lead"
                                self._scroll_timer = SCROLL_LEAD
                    else:
                        self._scroll_x = 0.0
                        self._scroll_phase = "lead"
                        self._scroll_timer = SCROLL_LEAD
            self._tick_render()
        # 看护：锁定时（或任务栏模式下）绝不允许窗口“消失”
        if (self._locked or self._docked) and not self._hidden_intentional:
            try:
                if not self.isVisible() or self.isMinimized():
                    self.show()
                    if self.isMinimized():
                        self.showNormal()
                    self.raise_()
                    self._force_topmost()
                elif self._docked and not self._drag and not self._resize:
                    # 周期性地压回任务栏之上（每约 0.6 秒一次，
                    # 与“悬停一下再点就不消失”的观察一致：缩短空白窗口期）
                    if now - self._last_force_top > 0.6:
                        self._last_force_top = now
                        self._force_topmost()
            except Exception:
                pass
        self._last_clock = now
        if self._notice and time.time() > self._notice_until:
            self._notice = ""
            self.update()

    def _reset_marquee(self):
        self._scroll_x = 0.0
        self._scroll_phase = "lead"
        self._scroll_timer = SCROLL_LEAD

    def _reset_draw_cache(self):
        self._seg_key = ("", "")
        self._seg_cache.clear()
        self._seg_widths.clear()

    # ==================================================================
    # 渲染入口
    # ==================================================================
    def _refresh_text(self, force: bool = False):
        m = self._media
        if m is None:
            new_cur = STATUS_WAIT
        elif not self._lines:
            if not self._fetch_empty and self._fetch_key != self._media_key:
                new_cur = STATUS_WAIT
            else:
                title = m.title or "未知歌曲"
                artist = m.artist or "未知歌手"
                note = "  （未找到歌词）" if self._fetch_empty else ""
                new_cur = f"♪  {title}  ·  {artist}{note}"
        else:
            idx = max(0, min(self._index, len(self._lines) - 1))
            new_cur = self._lines[idx].text or "♪"

        if not force and new_cur == self._cur_txt:
            return
        changed = new_cur != self._cur_txt
        self._cur_txt = new_cur
        if changed:
            self._reset_draw_cache()
            self._reset_marquee()
            self.update()
            if DEBUG:
                print(f"[ui] 行[{self._index}]: {self._cur_txt!r}", flush=True)

    def _cur_text_raw(self) -> str:
        """当前行原始文本（不做换行处理）。"""
        if self._lines and 0 <= self._index < len(self._lines):
            return self._lines[self._index].text or ""
        return ""

    def _visible_rows(self) -> int:
        if self._docked:
            return 1
        return max(1, self.height() // ROW_H[self._scale])

    # ==================================================================
    # 尺寸
    # ==================================================================
    def _font(self, px: int, bold: bool) -> QFont:
        f = QFont(FONT_FAMILY)
        f.setPixelSize(max(8, px))
        f.setBold(bold)
        return f

    def _auto_size(self):
        if self._manual or self._docked:
            return
        self.resize(self._preferred_width(), AUTO_PANEL_H)
        if self._use_center:
            self._center_on_screen()
        else:
            self._clamp_into_screen()
        self._reset_draw_cache()
        self.update()

    def _preferred_width(self) -> int:
        texts: list[str] = []
        if self._lines:
            texts = [l.text for l in self._lines if l.text]
        if not texts:
            texts = [self._cur_txt or STATUS_WAIT]

        px = FONT_PX[self._scale]
        fm = QFontMetrics(self._font(px, False))
        widths = sorted(fm.horizontalAdvance(t) for t in texts)
        if not widths:
            widths = [600]
        p95 = widths[min(len(widths) - 1, int(len(widths) * 0.95))]

        sc = self._screen()
        avail = sc.availableGeometry().width() - 60 if sc else 2500
        if self._long_mode == "grow":
            hi = min(GROW_CAP, avail)
        else:
            hi = min(WRAP_CAP, avail)
        w = int(p95 + (PAD_X + 8) * 2)
        return max(AUTO_W_LO, min(w, hi))

    def _text_max_w(self) -> int:
        return self.width() - 2 * PAD_X

    # ==================================================================
    # 文字换行缓存
    # ==================================================================
    def _segments(self, text: str) -> list[str]:
        max_w = self._text_max_w()
        px = FONT_PX[self._scale]
        key = (max_w, px)
        if key != self._seg_key:
            self._seg_key = key
            self._seg_cache.clear()
        if not text:
            return [""]
        cached = self._seg_cache.get(text)
        if cached is not None:
            return cached

        font = self._font(px, False)
        fm = QFontMetrics(font)
        if fm.horizontalAdvance(text) <= max_w:
            segs = [text]
        else:
            words = text.split(" ")
            lines: list[str] = []
            cur = ""
            for w in words:
                piece = w if not cur else cur + " " + w
                if fm.horizontalAdvance(piece) <= max_w:
                    cur = piece
                    continue
                if cur:
                    lines.append(cur)
                    cur = ""
                if fm.horizontalAdvance(w) > max_w:
                    buf = ""
                    for ch in w:
                        if buf and fm.horizontalAdvance(buf + ch) > max_w:
                            lines.append(buf)
                            buf = ""
                        buf += ch
                    if buf:
                        cur = buf
                else:
                    cur = w
            if cur:
                lines.append(cur)
            segs = lines or [text]
        if len(self._seg_cache) > 800:
            self._seg_cache.clear()
        self._seg_cache[text] = segs
        return segs

    def _seg_offsets(self, segs: list[str], fm: QFontMetrics):
        """每段的累计起点宽度 -> (offsets, total)。"""
        offs = []
        acc = 0
        for s in segs:
            offs.append(acc)
            acc += fm.horizontalAdvance(s)
        return offs, acc

    # ==================================================================
    # 绘制
    # ==================================================================
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        rect = self.rect()
        alpha = int(235 * self._opacity)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(COL_BG[0], COL_BG[1], COL_BG[2], alpha))
        radius = 9 if self._docked else 14
        p.drawRoundedRect(rect, radius, radius)
        p.setPen(QPen(QColor(255, 255, 255, int(36 * self._opacity)), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0, 0, -1, -1), radius, radius)

        if self._media is None:
            self._paint_center(p, self._cur_txt, COL_MUTED, False)
        elif not self._lines or self._index < 0:
            self._paint_center(p, self._cur_txt, COL_MUTED, False)
        else:
            self._paint_lyrics(p)

        if not self._click_through:
            self._paint_grip(p)

        self._paint_notice(p)

    def _paint_notice(self, p: QPainter):
        """临时浮动提示（换歌词反馈等）。"""
        if not self._notice or time.time() > self._notice_until:
            self._notice = ""
            return
        f = self._font(11, False)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(self._notice)
        pad_x, pad_y = 14, 7
        w = min(tw + pad_x * 2, self.width() - 24)
        h = fm.height() + pad_y * 2
        x = (self.width() - w) // 2
        y = int(self.height() * 0.84) if self.height() > 100 else \
            max(2, (self.height() - h) // 2)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 21, 26, 225))
        p.drawRoundedRect(QRect(x, y, w, h), 8, 8)
        p.setPen(QColor("#d8dbe2"))
        p.setFont(f)
        text = fm.elidedText(self._notice, Qt.ElideMiddle, w - pad_x * 2)
        p.drawText(QRect(x, y, w, h), Qt.AlignCenter, text)
        p.restore()

    def _paint_center(self, p: QPainter, text: str, color: QColor, bold: bool):
        px = FONT_PX[self._scale]
        p.setFont(self._font(px, bold))
        p.setPen(color)
        p.drawText(QRect(PAD_X, 0, self.width() - 2 * PAD_X, self.height()),
                   Qt.AlignCenter, text)

    # ------------------------------------------------------------------
    # 歌词主体
    # ------------------------------------------------------------------
    def _paint_lyrics(self, p: QPainter):
        rows = self._visible_rows()
        if rows == 1:
            self._paint_single_row(p)
            return
        self._paint_list(p, rows)

    # ---------- 多行列表 ----------
    def _paint_list(self, p: QPainter, rows: int):
        w = self.width()
        h = self.height()
        px = FONT_PX[self._scale]
        row_h = ROW_H[self._scale]
        max_w = self._text_max_w()
        fm_cur = QFontMetrics(self._font(px, True))

        cur_idx = self._index
        segs_cur = self._segments(self._lines[cur_idx].text or "")
        k = len(segs_cur)

        # 当前行的位置：行数少时放最上面；行数多时固定在 ~45% 处
        if rows <= 2:
            anchor = 0
        else:
            anchor = max(0, min(int(rows * 0.45), rows - k))

        above = anchor
        below = max(0, rows - anchor - k)

        # 上方：尽量收集完整的前序行
        stack = []
        used = 0
        p_idx = cur_idx - 1
        while p_idx >= 0 and above - used > 0:
            cnt = len(self._segments(self._lines[p_idx].text or ""))
            if cnt > above - used:
                break
            stack.append(p_idx)
            used += cnt
            p_idx -= 1

        # 按行填充绘制块： (y起点行, 文本段, 颜色类型)
        drawn = []      # (y0_in_rows, segments, kind)
        y = anchor - used
        for idx in reversed(stack):
            segs = self._segments(self._lines[idx].text or "")
            drawn.append((y, segs, "past"))
            y += len(segs)
        drawn.append((y, segs_cur, "cur"))
        y += k

        n_idx = cur_idx + 1
        used_b = 0
        while n_idx < len(self._lines) and below - used_b > 0:
            segs = self._segments(self._lines[n_idx].text or "")
            if len(segs) > below - used_b:
                break
            drawn.append((y, segs, "future"))
            y += len(segs)
            used_b += len(segs)
            n_idx += 1

        # 当前行进度（跨所有段连续）
        frac = self._line_progress()
        offsets, total_w = self._seg_offsets(segs_cur, fm_cur)
        sung_px = total_w * frac

        for y0, segs, kind in drawn:
            for j, seg in enumerate(segs):
                top = y0 * row_h + j * row_h + int(self._slide_px)
                if top + row_h > h:
                    break  # 超出窗口底部的换行片段不画
                row = QRect(PAD_X, top, max_w, row_h)
                if kind == "cur":
                    if not self._karaoke:
                        p.setFont(self._font(px, True))
                        p.setPen(COL_SUNG_PAUSED if self._paused else COL_SUNG)
                        p.drawText(row, Qt.AlignLeft | Qt.AlignVCenter, seg)
                    else:
                        self._paint_progress(p, seg, row, px, sung_px - offsets[j])
                elif kind == "past":
                    p.setFont(self._font(px, False))
                    p.setPen(COL_PAST)
                    p.drawText(row, Qt.AlignLeft | Qt.AlignVCenter, seg)
                else:
                    p.setFont(self._font(px, False))
                    p.setPen(COL_FUTURE)
                    p.drawText(row, Qt.AlignLeft | Qt.AlignVCenter, seg)

    # ---------- 单行（含任务栏 / 矮窗口）----------
    def _paint_single_row(self, p: QPainter):
        text = self._lines[self._index].text or ""
        w = self.width()
        h = self.height()
        px = FONT_PX[self._scale]
        max_w = self._text_max_w()
        fm = QFontMetrics(self._font(px, True))
        tw = fm.horizontalAdvance(text)

        frac = self._line_progress()
        sung_px = tw * frac
        row = QRect(PAD_X, 0, max_w, h)

        if tw <= max_w:
            # 放得下：居中 + 卡拉OK着色
            x0 = row.left() + (max_w - tw) // 2
            if not self._karaoke:
                p.setFont(self._font(px, True))
                p.setPen(COL_SUNG_PAUSED if self._paused else COL_SUNG)
                p.drawText(row, Qt.AlignCenter, text)
                return
            self._paint_progress_at(p, text, x0, fm, px, sung_px)
            return
        # 放不下：横向滚动（暂停时冻结在句首）
        x = 0.0 if self._paused else self._scroll_x
        p.save()
        p.setClipRect(row)
        baseline = row.top() + (row.height() - fm.height()) / 2 + fm.ascent()
        p.setFont(self._font(px, True))
        if not self._karaoke:
            p.setPen(COL_SUNG_PAUSED if self._paused else COL_SUNG)
            p.drawText(row.left() + int(x), int(baseline), text)
        else:
            self._paint_progress_chars(p, text, row.left() + int(x),
                                       int(baseline), fm, sung_px)
        p.restore()

    # ---------- 卡拉OK逐字着色 ----------
    def _paint_progress(self, p: QPainter, text: str, row: QRect, px: int,
                        sung_px: float):
        """在某一行矩形内按行垂直居中绘制带进度的文本（左对齐）。"""
        fm = QFontMetrics(self._font(px, True))
        p.setFont(self._font(px, True))
        baseline = row.top() + (row.height() - fm.height()) / 2 + fm.ascent()
        self._paint_progress_chars(p, text, row.left(), int(baseline), fm,
                                   sung_px)

    def _paint_progress_at(self, p: QPainter, text: str, x: int, fm: QFontMetrics,
                           px: int, sung_px: float):
        p.setFont(self._font(px, True))
        y = self.height() // 2 - fm.height() // 2 + fm.ascent()
        self._paint_progress_chars(p, text, x, y, fm, sung_px)

    def _paint_progress_chars(self, p: QPainter, text: str, x0: int,
                              baseline: int, fm: QFontMetrics, sung_px: float):
        """KTV 式平滑扫光。

        每个字只绘制一次；跨越“高亮边界”的那个字会被左右各裁剪一半，
        分别用高亮色/未亮色绘制，边界是连续的（不会一整字一整字地跳）。
        """
        if not text:
            return
        px = FONT_PX[self._scale]
        p.setFont(self._font(px, True))
        if self._paused:
            sung_color = COL_SUNG_PAUSED
            rest_color = COL_REST
        else:
            sung_color = COL_SUNG
            rest_color = COL_REST

        ascent = fm.ascent()
        descent = fm.descent()
        gh = ascent + descent
        acc = 0.0
        for ch in text:
            cw = fm.horizontalAdvance(ch)
            start = acc
            end = acc + cw
            x = x0 + int(start)
            if end <= sung_px:
                p.setPen(sung_color)
                p.drawText(x, baseline, ch)
            elif start >= sung_px:
                p.setPen(rest_color)
                p.drawText(x, baseline, ch)
            else:
                # 边界字：左半高亮 / 右半未亮（两次裁剪互不重叠，无锯齿边）
                cut = max(1, int(sung_px - start))
                p.save()
                p.setClipRect(x, baseline - ascent, cut, gh)
                p.setPen(sung_color)
                p.drawText(x, baseline, ch)
                p.restore()
                if cut < cw:
                    p.save()
                    p.setClipRect(x + cut, baseline - ascent, cw - cut, gh)
                    p.setPen(rest_color)
                    p.drawText(x, baseline, ch)
                    p.restore()
            acc = end

    # ---------- 把手 ----------
    def _paint_grip(self, p: QPainter):
        x0 = self.width() - 14
        y0 = self.height() - 14
        p.setPen(QPen(QColor(150, 156, 168, int(150 * self._opacity)), 1))
        for i in range(3):
            x = x0 + i * 3
            y = y0 + i * 3
            p.drawLine(x - 5, y + 5, x + 5, y - 5)

    # ==================================================================
    # 几何 / 屏幕
    # ==================================================================
    def _screen(self):
        return QApplication.screenAt(self.mapToGlobal(self.rect().center())) \
            or QApplication.primaryScreen()

    def _center_on_screen(self):
        sc = self._screen()
        if sc is None:
            return
        g = sc.availableGeometry()
        x = g.center().x() - self.width() // 2
        x = max(g.left(), min(x, g.right() - self.width()))
        y = max(g.top(), min(self.y(), g.bottom() - self.height()))
        self.move(x, y)

    def _clamp_into_screen(self):
        sc = self._screen()
        if sc is None:
            return
        g = sc.availableGeometry()
        x = max(g.left() - self.width() + 60, min(self.x(), g.right() - 60))
        y = max(g.top(), min(self.y(), g.bottom() - self.height()))
        self.move(x, y)

    def _grip_rect(self) -> QRect:
        return QRect(self.width() - 24, self.height() - 24, 24, 24)

    def _save_geometry(self):
        self._settings.setValue("pos", self.pos())
        self._settings.setValue("w", self.width())
        self._settings.setValue("h", self.height())
        self._settings.setValue("manual", "true" if self._manual else "false")

    def _restore_geometry(self):
        pos = self._settings.value("pos")
        if self._docked:
            self._setup_dock()
            self._dock_resize_restore()
            return
        if pos is not None:
            self._use_center = self._settings.value("usecenter", "true") == "true"
        if self._manual and self._win_w >= MIN_W and self._win_h >= MIN_H:
            self.resize(self._win_w, self._win_h)
        else:
            self.resize(self._preferred_width(), AUTO_PANEL_H)
        if pos is not None:
            self.move(int(pos.x()), int(pos.y()))
        else:
            self._center_on_screen()

    def _restore_geometry_from_saved(self):
        w = int(self._settings.value("w", self.width()))
        h = int(self._settings.value("h", self.height()))
        self._manual = self._settings.value("manual", "false") == "true"
        if self._manual and w >= MIN_W and h >= MIN_H:
            self.resize(w, h)
        else:
            self._auto_size()
        pos = self._settings.value("pos")
        if pos is not None:
            self.move(int(pos.x()), int(pos.y()))

    # ==================================================================
    # 鼠标
    # ==================================================================
    def mousePressEvent(self, event: QMouseEvent):
        if self._click_through or event.button() != Qt.LeftButton:
            return
        if self._grip_rect().contains(event.pos()):
            self._resize = (event.globalPos(), self.width(), self.height(),
                            self.x() + self.width() // 2, self.y())
            event.accept()
            return
        self._drag = event.globalPos() - self.frameGeometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._click_through:
            return
        if self._resize is not None:
            origin, ow, oh, cx, oy = self._resize
            dx = event.globalPos().x() - origin.x()
            dy = event.globalPos().y() - origin.y()
            sc = self._screen()
            max_w = sc.availableGeometry().width() - 40 if sc else 3000
            max_h = int(sc.availableGeometry().height() * 0.9) if sc else 900
            nw = max(MIN_W, min(ow + dx, max_w))
            nh = max(MIN_H, min(oh + dy, max_h))
            self._manual = True
            if self._docked:
                nh = self._dock_h
                if sc is not None:
                    nw = min(nw, sc.geometry().width() - 80)
                self.resize(nw, nh)
                lo = 2
                hi = (sc.geometry().right() - nw - 2) if sc else 4000
                self.move(max(lo, min(cx - nw // 2, hi)), self._dock_y)
            else:
                self.resize(nw, nh)
            self._reset_draw_cache()
            self.update()
            event.accept()
            return
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            if self._docked:
                sc = self._screen()
                if sc is not None:
                    lo = sc.geometry().left() + 2
                    hi = sc.geometry().right() - self.width() - 2
                else:
                    lo, hi = 2, 4000
                self.move(max(lo, min(event.globalPos().x() - self._drag.x(), hi)),
                          self._dock_y)
            else:
                self.move(event.globalPos() - self._drag)
            event.accept()
            return
        if self._grip_rect().contains(event.pos()):
            self.setCursor(Qt.SizeFDiagCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._resize is not None:
            self._resize = None
            self._manual = True
            self._settings.setValue("manual", "true")
            self._settings.setValue("w", self.width())
            self._settings.setValue("h", self.height())
            self._settings.setValue("pos", self.pos())
            self._reset_draw_cache()
            self.update()
            event.accept()
            return
        if self._drag is not None:
            self._drag = None
            if self._use_center:
                self._use_center = False
                self._settings.setValue("usecenter", "false")
            self._settings.setValue("pos", self.pos())
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if (event.button() == Qt.LeftButton and not self._click_through
                and not self._docked):
            # 锁定时双击也不隐藏（走 _toggle_visible 的锁定保护）
            self._toggle_visible()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reset_draw_cache()
        self.update()

    def contextMenuEvent(self, event):
        self._popup_menu()

    def closeEvent(self, event):
        event.ignore()
        self._hidden_intentional = True
        self.hide()

    # ==================================================================
    # 任务栏吸附
    # ==================================================================
    def _setup_dock(self):
        sc = QApplication.primaryScreen()
        if sc is None:
            self._dock_w, self._dock_h = 800, 44
            self._dock_x, self._dock_y = 0, 0
            return
        full = sc.geometry()
        avail = sc.availableGeometry()
        band = full.height() - avail.height()
        if band < 6:
            band = 44
        self._dock_h = max(30, band - 4)
        self._dock_w = min(int(full.width() * 0.8), 1180)
        self._dock_y = full.bottom() - band + (band - self._dock_h) // 2
        self._dock_x = full.center().x() - self._dock_w // 2

    def _dock_resize_restore(self):
        nw = self._win_w if self._manual and self._win_w >= MIN_W else self._preferred_width()
        nw = max(MIN_W, min(nw, self._dock_w))
        self.resize(nw, self._dock_h)
        sc = QApplication.primaryScreen()
        full = sc.geometry() if sc else None
        cx = full.center().x() if full else self._dock_x + nw // 2
        lo = 2 if full else 0
        hi = (full.right() - nw - 2) if full else 4000
        self.move(max(lo, min(cx - nw // 2, hi)), self._dock_y)
