"""读取 Apple Music (Windows Store 版) 自己缓存的 TTML 歌词。

原理：Apple Music 显示过的歌词会以 `ttmlLyrics*.json` 落在它自己的
WinINet 缓存（AC\\INetCache），内容是 Apple 官方带逐行 begin/end 的 TTML；
同一缓存里还有 lookup/search/library 等接口响应，记录了歌曲
id/名称/艺人/时长。两边用 `AP_<歌曲id>` + 名称/时长做关联，即可拿到
“Apple Music 自己显示的那份歌词”——LRCLib/网易云匹配不好时的最佳兜底。

说明：
- 只读本机自己的缓存，不联网、不改动任何文件；
- 缓存会被 Apple Music 按 LRU 淘汰，仅近期显示过歌词的歌可命中；
- 命中后返回与官方显示一致的同步歌词（含简体翻译优先逻辑）。
"""

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET

from lyrics_fetcher import LyricLine

_PKG = "AppleInc.AppleMusicWin_nzyj5cx40ttqa"
_REFRESH_SECONDS = 90


def _cache_root() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages",
                        _PKG, "AC", "INetCache")


class AppleLocalLyrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._lyrics: dict[int, dict] = {}     # 歌曲id -> 合并后的分页原文/译文
        self._songs: dict[int, dict] = {}      # 歌曲id -> 名称/艺人/时长
        self._finalized_ids: set = set()       # 已完成行合成的 id
        self._built_at = 0.0

    # ------------------------------------------------------------------
    def lookup(self, title: str, artist: str, duration_ms: int):
        """返回 (lines, label, detail) 或 None。title/artist 已清洗。"""
        try:
            self._ensure_index()
        except Exception:
            return None
        cands = []
        for sid, lyr in self._lyrics.items():
            lines = lyr.get("lines") or []
            if len(lines) < 8:
                continue  # 过短：缓存多半只有一两页，放弃（避免“寥寥几句”）
            song = self._songs.get(sid)
            if song is None:
                continue
            sd = int(song.get("duration_ms") or 0)
            if sd > 0 and lines[-1].time_ms < sd * 0.5 and len(lines) < 35:
                continue  # 明显只缓存了前半段
            score = 0.0
            rt, rn = _norm(song.get("title", "")), _norm(title)
            arts = [a.lower() for a in song.get("artists", [])]
            if rn == rt:
                score += 50
            elif rt and (rn.startswith(rt) or rt.startswith(rn)):
                score += 30
            elif rt:
                score += _ratio(rn, rt) * 25
            if arts and artist:
                hit = any(_norm(a) == _norm(b) for a in arts
                          for b in _clean_artist_tokens(artist))
                score += 35 if hit else -20
            if duration_ms > 0 and sd > 0:
                diff = abs(sd - duration_ms)
                score += 20 if diff <= 1500 else (-15 if diff <= 5000 else -80)
            if score >= 55:
                cands.append((score, sid, lyr, song))
        if not cands:
            return None
        cands.sort(key=lambda x: x[0], reverse=True)
        score, sid, lyr, song = cands[0]
        lines = lyr.get("lines") or []
        if not lines:
            return None
        label = "Apple Music 本地"
        if song.get("title") and song.get("artists"):
            label += f"·{song['title']}"
        return list(lines), label, {"score": round(score, 1), "sid": sid}

    # ------------------------------------------------------------------
    def _ensure_index(self):
        now = time.time()
        with self._lock:
            if now - self._built_at < _REFRESH_SECONDS and self._lyrics:
                return
            self._build()
            self._built_at = now

    def _build(self):
        self._lyrics = {}
        self._songs = {}
        self._finalized_ids = set()
        root = _cache_root()
        if not os.path.isdir(root):
            return
        for dirpath, _, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    if size <= 0 or size > 3_000_000:
                        continue
                    if name.startswith("ttmlLyrics"):
                        self._parse_lyrics_file(path)
                    elif size < 1_000_000:
                        self._parse_catalog_file(path, name)
                except OSError:
                    continue
                except Exception:
                    continue
        self._finalize()

    def _finalize(self):
        """把逐页合并结果合成为完整行列表（带简中译文优先）。"""
        for sid, agg in list(self._lyrics.items()):
            if sid in self._finalized_ids:
                continue
            by_key = agg.get("by_key") or {}
            tr = agg.get("translation") or {}
            use_tr = len(tr) >= max(1, int(len(by_key) * 0.6))
            items = []
            for key, item in by_key.items():
                text = (tr.get(key) if use_tr else None) or item.get("text")
                if not text:
                    continue
                items.append((item.get("begin", 0.0), text))
            items.sort(key=lambda x: x[0])
            agg["lines"] = [LyricLine(time_ms=int(b * 1000), text=t)
                            for b, t in items]
            self._finalized_ids.add(sid)

    # ------------------------------------------------------------------
    def _parse_lyrics_file(self, path: str):
        try:
            data = json.load(open(path, encoding="utf-8", errors="ignore"))
        except Exception:
            return
        lyrics_id = (data.get("lyricsId") or "").strip()
        m = re.search(r"(\d+)$", lyrics_id)
        if not m:
            return
        sid = int(m.group(1))
        ttml = data.get("ttml") or ""
        parsed = _parse_ttml(ttml)
        if parsed and parsed.get("by_key"):
            # 同歌曲的 TTML 会按“页”拆成多个缓存文件，逐页合并原文与译文
            agg = self._lyrics.setdefault(sid, {
                "by_key": {}, "translation": {}, "silence": 0.0})
            agg["by_key"].update(parsed["by_key"])
            agg["translation"].update(parsed["translation"] or {})
            if parsed.get("silence"):
                agg["silence"] = parsed["silence"]
        self._finalized_ids.discard(sid)

    def _parse_catalog_file(self, path: str, name: str):
        try:
            raw = open(path, encoding="utf-8", errors="ignore").read(1_000_000)
        except Exception:
            return
        if "durationInMillis" not in raw and "hasTimeSyncedLyrics" not in raw:
            return
        if "durationInMillis" not in raw and name.endswith(".json") is False:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        self._walk_catalog(data)

    def _walk_catalog(self, o):
        if isinstance(o, dict):
            attrs = o.get("attributes")
            if isinstance(attrs, dict) and ("name" in attrs) and (
                    "durationInMillis" in attrs or "trackDurationInMillis" in attrs):
                sid = o.get("id")
                if sid is None:
                    sid = (attrs.get("playParams") or {}).get("catalogId")
                sid = str(sid or "").strip()
                if sid and sid.isdigit():
                    title = attrs.get("name") or ""
                    artists = []
                    if attrs.get("artistName"):
                        artists = _clean_artist_tokens(attrs["artistName"])
                    dur = attrs.get("durationInMillis") \
                        or attrs.get("trackDurationInMillis") or 0
                    self._songs[int(sid)] = {
                        "title": title,
                        "artists": artists,
                        "duration_ms": int(dur),
                    }
            for v in o.values():
                self._walk_catalog(v)
        elif isinstance(o, list):
            for v in o:
                self._walk_catalog(v)


# ----------------------------------------------------------------------------
# 名称清洗 / 相似度（与 lyrics_fetcher 保持一致的逻辑）
# ----------------------------------------------------------------------------
_SUFFIX_RE = re.compile(
    r"(?i)\s*[-–—·]?\s*(single|ep|album|deluxe\s*edition|remaster(ed)?|"
    r"live|acoustic|radio\s*edit|clean|explicit|instrumental|soundtrack|"
    r"theme\s*song|demo|reprise|bonus\s*track)\s*$")
_BRACKET_RE = re.compile(
    r"(?i)\s*[\(\[（【][^\)\]）】]*(?:live|acoustic|remix|feat\.?|with|"
    r"demo|cover|karaoke|伴奏|现场)[^\)\]）】]*[\)\]）】]\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _clean_artist_tokens(artist: str):
    raw = artist or ""
    raw = re.sub(_BRACKET_RE, "", raw)
    parts = []
    for chunk in re.split(r"[&,，、/;；]", raw):
        chunk = re.sub(r"(?i)(feat\.?|ft\.?|with)\s+.*$", "", chunk).strip(" -")
        chunk = re.sub(_SUFFIX_RE, "", chunk).strip()
        if chunk:
            parts.append(chunk)
    return parts


# ----------------------------------------------------------------------------
# TTML 解析
# ----------------------------------------------------------------------------
def _parse_ttml(ttml: str):
    """解析一页 Apple TTML，返回按 key 组织的原文/译文（由调用方合并多页）。"""
    try:
        root = ET.fromstring(ttml)
    except Exception:
        # 容错：极端情况直接按文本块兜底
        return {"by_key": {}, "translation": {}, "silence": 0.0}

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    silence = 0.0
    for el in root.iter():
        if local(el.tag) == "iTunesMetadata":
            for ch in el.iter():
                if local(ch.tag) == "leadingSilence":
                    try:
                        silence = float((ch.text or "").strip())
                    except ValueError:
                        pass

    by_key = {}
    translation = {}
    for p in root.iter():
        if local(p.tag) != "p":
            continue
        key = (p.get("{http://music.apple.com/lyric-ttml-internal}key")
               or p.get("itunes:key") or p.get("key"))
        begin = _parse_sec(p.get("begin"))
        end = _parse_sec(p.get("end"))
        if key is None or begin is None:
            continue
        text = "".join(p.itertext()).strip()
        if key not in by_key:
            by_key[key] = {"begin": begin, "end": end, "text": text}

    # 简中翻译（也是分页的，逐页收集后由 _finalize 统一判定覆盖率）
    for tr in root.iter():
        if local(tr.tag) == "translation" and (tr.get("xml:lang")
                                               or tr.get("lang")) in (
                "zh-Hans", "zh-Hans-CN", "zh-CN"):
            for t in tr.iter():
                if local(t.tag) == "text":
                    translation[t.get("for")] = "".join(t.itertext()).strip()

    return {"by_key": by_key, "translation": translation, "silence": silence}


def _parse_sec(s):
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
