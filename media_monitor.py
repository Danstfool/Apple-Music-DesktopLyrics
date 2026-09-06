"""轮询 Windows 系统媒体会话（SMTC），只关注 Apple Music / iTunes。

运行在独立线程的 asyncio 事件循环中，通过回调把 MediaInfo 交给上层。
回调会被频繁调用（每个轮询周期一次），因此回调必须轻量。
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

# Apple Music (Microsoft Store 版) 的 AUMID 形如:
#   AppleInc.AppleMusicWin_nzyj5cx40ttqa!App
APP_MARKERS = ("apple", "itunes")


def _log(*args):
    print("[monitor]", *args, file=sys.stderr, flush=True)


@dataclass
class MediaInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    position_ms: int = 0
    is_playing: bool = False
    app_name: str = ""

    @property
    def song_key(self) -> str:
        return f"{self.title}\x1f{self.artist}\x1f{self.duration_ms}"


def _time_ms(value) -> int:
    """把 Windows TimeSpan 转成毫秒。

    winrt >= 2.0 把它投影成 datetime.timedelta；
    老版本 pywinrt / winsdk 是带 .duration（100ns 单位）的结构体。
    """
    if value is None:
        return 0
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1000)
    ticks = getattr(value, "duration", None)
    if ticks is not None:
        return int(ticks) // 10000
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        return int(total_seconds() * 1000)
    return 0


class MediaMonitor:
    def __init__(self, on_update: Callable, poll_interval: float = 0.5):
        self._on_update = on_update
        self._interval = poll_interval
        self._running = False
        self._manager = None
        self._apple_session = None
        self._notified_idle = False
        self._last_key = ""

    def start(self):
        """阻塞运行，应在独立线程里调用。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            _log("monitor crashed:", repr(e))

    def stop(self):
        self._running = False

    async def _run(self):
        try:
            self._manager = (
                await GlobalSystemMediaTransportControlsSessionManager.request_async()
            )
        except Exception as e:
            _log("无法连接系统媒体会话(SMTC):", repr(e))
            return
        _log("SMTC 就绪，开始监听 Apple Music")
        self._running = True
        while self._running:
            try:
                await self._poll()
            except Exception as e:
                _log("轮询出错:", repr(e))
            await asyncio.sleep(self._interval)

    async def _poll(self):
        current = self._manager.get_current_session()
        aumid = (current.source_app_user_model_id or "") if current is not None else ""
        is_apple = current is not None and any(m in aumid.lower() for m in APP_MARKERS)

        if is_apple:
            self._notified_idle = False
            self._apple_session = current
            info = await self._read(current, aumid)
            if info is None:
                return
            key = info.song_key
            if key != self._last_key:
                self._last_key = key
                _log(f"检测到新歌曲: {info.title} - {info.artist}")
            self._on_update(info)
        else:
            self._apple_session = None
            if not self._notified_idle:
                self._notified_idle = True
                self._last_key = ""
                _log("Apple Music 未在播放，进入待机")
                self._on_update(None)

    async def _read(self, session, aumid: str) -> Optional[MediaInfo]:
        try:
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            timeline = session.get_timeline_properties()
            status = playback.playback_status

            info = MediaInfo(
                title=(props.title or "").strip(),
                artist=(props.artist or "").strip(),
                album=(props.album_title or "").strip(),
                duration_ms=_time_ms(timeline.end_time),
                position_ms=_time_ms(timeline.position),
                is_playing=status == PlaybackStatus.PLAYING,
                app_name=aumid,
            )
            return info
        except Exception as e:
            _log("读取媒体信息失败:", repr(e))
            return None
