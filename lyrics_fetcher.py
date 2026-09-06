"""多源歌词抓取（LRCLib -> 网易云），带匹配度校验与候选池。

目的：避免“显示成别的歌的歌词”和“版本时间对不上”：
- 清洗 Apple 传来的标题/艺人（去掉 “- Single / - EP / (Live)” 等干扰后缀、拆分多艺人）
- 对每个候选做 艺人/标题/时长 三维打分，时长差太多直接降权
- 一首歌保留多份候选，UI 可在菜单里“换下一份歌词”
"""

import re
import sys
import threading
from dataclasses import dataclass
from typing import Optional

import requests

UA = "DesktopLyrics/2.1 (Windows; Apple Music desktop lyrics)"

MAX_CANDIDATES = 5

_DUR_TOLERANCE_MS = 5000   # 与 Apple 时长相差超过此值视为“另一个版本/另一首歌”


def _log(*args):
    print("[lyrics]", *args, file=sys.stderr, flush=True)


@dataclass
class LyricLine:
    time_ms: int
    text: str


# ----------------------------------------------------------------------------
# 文本清洗
# ----------------------------------------------------------------------------
_SUFFIX_RE = re.compile(
    r"(?i)\s*[-–—·]?\s*(single|ep|album|deluxe\s*edition|remaster(ed)?|"
    r"live|acoustic|radio\s*edit|clean|explicit|instrumental|original\s*motion\s*"
    r"picture\s*soundtrack|official\s*soundtrack|soundtrack|theme\s*song|"
    r"demo|reprise|bonus\s*track)\s*$"
)
_BRACKET_RE = re.compile(r"(?i)\s*[\(\[（【][^\)\]）】]*(?:live|acoustic|remix|"
                         r"feat\.?|with|demo|cover|karaoke|伴奏|现场|live版)[^\)\]）】]*[\)\]）】]\s*$")
_ARTIST_SPLIT = re.compile(r"(?i)[&,，、/;；\s]+(?:feat\.?|ft\.?|with|和|&)?\s*")
_ALBUM_SEP = re.compile(r"\s*[—–]\s*")
_VERSION_WORDS = re.compile(r"(?i)live|acoustic|remix|现场|伴奏|翻唱|cover|remaster|demo")


def clean_title(title: str) -> str:
    t = title or ""
    t = re.sub(_BRACKET_RE, "", t)
    t = re.sub(_SUFFIX_RE, "", t)
    return t.strip()


def clean_artist(artist: str):
    """拆分多艺人并清洗，返回 ['吴青峰','陈粒'] 之类的小写 token 列表。

    注意：Apple SMTC 的 artist 字段可能是“歌手 — 专辑”或
    “歌手 - Single”的形式，需要先把 —— 之后的专辑名剥掉。
    """
    raw = artist or ""
    raw = re.sub(_BRACKET_RE, "", raw)
    # “歌手 — 专辑”：取 —— 前的主体部分
    raw = _ALBUM_SEP.split(raw)[0]
    parts = []
    for chunk in re.split(r"[&,，、/;；]", raw):
        chunk = re.sub(r"(?i)(feat\.?|ft\.?|with)\s+.*$", "", chunk).strip(" -")
        chunk = re.sub(_SUFFIX_RE, "", chunk).strip()
        if chunk:
            parts.append(chunk)
    return parts or [raw.strip()]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


# ----------------------------------------------------------------------------
class _Candidate:
    __slots__ = ("score", "label", "lines")

    def __init__(self, score: float, label: str, lines: list):
        self.score = score
        self.label = label
        self.lines = lines


class LyricsFetcher:
    def __init__(self):
        # key -> [候选(已按质量排序)]
        self._pool: dict[str, list[_Candidate]] = {}
        self._pick: dict[str, int] = {}     # key -> 当前候选序号
        self._lock = threading.Lock()
        self._apple_local = None

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------
    @staticmethod
    def key(title, artist, duration_ms=0):
        return f"{clean_title(title)}\x1f{clean_artist(artist)}\x1f{duration_ms}"

    def fetch(self, title: str, artist: str, duration_ms: int = 0) -> list:
        return self.fetch_streaming(title, artist, duration_ms, None)

    def fetch_streaming(self, title: str, artist: str, duration_ms: int = 0,
                        on_candidate=None) -> list:
        """四路并行抓取 + 先到先显示。

        on_candidate(lines, label) 会被调用：
        1) 一旦有“高质量”候选完成（通常 Apple 本地或 LRCLib 首个返回，
           几百毫秒内就能显示歌词）；
        2) 全部来源完成后，若第一名发生变化再补发一次最终结果。
        """
        key = self.key(title, artist, duration_ms)
        with self._lock:
            cached = self._pool.get(key)
            cur = self._pick.get(key, 0) if cached else 0
        if cached is not None:
            c = cached[cur % len(cached)]
            if on_candidate:
                on_candidate(list(c.lines), c.label)
            return list(c.lines) if cached else []

        c_title = clean_title(title)
        c_artists = clean_artist(artist)
        merged: list[_Candidate] = []
        emitted_sig = None
        emit_lock = threading.Lock()

        def emit_if_strong():
            nonlocal emitted_sig
            if on_candidate is None:
                return
            top = merged[0] if merged else None
            if top is None or top.score < 60:
                return
            sig = self._sig(top)
            with emit_lock:
                if emitted_sig == sig:
                    return
                emitted_sig = sig
            try:
                on_candidate(list(top.lines), top.label)
            except Exception:
                pass

        def run(task):
            try:
                return task()
            except Exception as e:
                _log("并行抓取任务异常:", repr(e))
                return []

        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=5,
                                   thread_name_prefix="lyr") as ex:
            futs = [
                ex.submit(run, lambda: self._gather_apple(c_title, c_artists,
                                                          duration_ms)),
                ex.submit(run, lambda: self._gather_lrclib_get(
                    c_title, c_artists, duration_ms)),
                ex.submit(run, lambda: self._gather_lrclib_search(
                    c_title, c_artists, duration_ms)),
                ex.submit(run, lambda: self._gather_netease(
                    title, c_title, c_artists, duration_ms)),
                ex.submit(run, lambda: self._gather_qq(
                    c_title, c_artists, duration_ms)),
            ]
            for fut in cf.as_completed(futs):
                part = fut.result()
                if part:
                    merged = self._merge(merged, part)
                    emit_if_strong()

        if not merged:
            _log(f"未找到可用歌词: {title} - {artist}")
        elif merged[0].score < 20:
            _log(f"候选质量偏低(仍保留): {merged[0].label} "
                 f"score={merged[0].score:.0f}")
        with self._lock:
            self._pool[key] = merged
            self._pick.pop(key, None)

        top = merged[0] if merged else None
        if top and on_candidate:
            sig = self._sig(top)
            with emit_lock:
                changed = emitted_sig != sig
                emitted_sig = sig
            if changed:
                try:
                    on_candidate(list(top.lines), top.label)
                except Exception:
                    pass
        return list(top.lines) if top else []

    @staticmethod
    def _sig(c: _Candidate) -> str:
        return c.label + "|" + "|".join(l.text for l in c.lines[:12])

    @staticmethod
    def _merge(base: list, extra: list) -> list:
        """合并候选并去重排序。"""
        out = list(base)
        seen = {LyricsFetcher._sig(c) for c in out}
        for c in extra:
            s = LyricsFetcher._sig(c)
            if s in seen:
                continue
            seen.add(s)
            out.append(c)
        out.sort(key=lambda c: c.score, reverse=True)
        return out[:MAX_CANDIDATES]

    # ------------------------------------------------------------------
    # 各来源的候选收集（可并行执行，互不依赖）
    # ------------------------------------------------------------------
    def _gather_apple(self, c_title, c_artists, duration_ms) -> list:
        out = []
        try:
            from apple_local import AppleLocalLyrics
            if self._apple_local is None:
                self._apple_local = AppleLocalLyrics()
            res = self._apple_local.lookup(c_title, " / ".join(c_artists),
                                           duration_ms)
            if res:
                lines, label, _detail = res
                out.append(_Candidate(999.0, label, lines))
        except Exception as e:
            _log("apple local:", repr(e))
        return out

    def _gather_lrclib_get(self, c_title, c_artists, duration_ms) -> list:
        out = []
        for rec, label in self._lrclib_get(c_title, c_artists, duration_ms):
            s = self._score_record(rec, c_title, c_artists, duration_ms)
            if self._clearly_other_song(rec, c_artists, duration_ms):
                continue
            if rec.get("syncedLyrics"):
                out.append(_Candidate(s + 30, label + "·同步",
                                      self.parse_lrc(rec["syncedLyrics"])))
            elif rec.get("plainLyrics"):
                out.append(_Candidate(s - 20, label + "·纯文本",
                                      self.parse_plain(rec["plainLyrics"])))
        return out

    def _gather_lrclib_search(self, c_title, c_artists, duration_ms) -> list:
        out = []

        def consume(records, tag):
            for rec, label in records:
                s = self._score_record(rec, c_title, c_artists, duration_ms)
                if s < -50 or self._clearly_other_song(rec, c_artists,
                                                       duration_ms):
                    continue
                if rec.get("syncedLyrics"):
                    out.append(_Candidate(s + 20, label,
                                          self.parse_lrc(rec["syncedLyrics"])))
                elif rec.get("plainLyrics"):
                    out.append(_Candidate(s - 25, label + "·纯文本",
                                          self.parse_plain(rec["plainLyrics"])))

        consume(self._lrclib_search(c_title, c_artists), "LRCLib")
        if not out:
            # 带艺人搜不到时，退一步只按标题搜（防止艺人写法差异漏歌）
            consume(self._lrclib_search(c_title, []), "LRCLib·仅标题")
        return out

    def _gather_netease(self, title, c_title, c_artists, duration_ms) -> list:
        out = []
        for rec in self._netease(title, c_title, c_artists, duration_ms):
            if rec.get("syncedLyrics"):
                out.append(_Candidate(rec["score"] + 10, "网易云·同步",
                                      self.parse_lrc(rec["syncedLyrics"])))
            else:
                out.append(_Candidate(rec["score"] - 10, "网易云",
                                      self.parse_plain(rec.get("plainLyrics", ""))))
        return out

    def _gather_qq(self, c_title, c_artists, duration_ms) -> list:
        out = []

        def run_query(query: str) -> list:
            """QQ 搜索 -> 返回 [(score, song)]。"""
            try:
                r = requests.get(
                    "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                    params={"format": "json", "p": 1, "n": 15, "w": query},
                    headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
                    timeout=(2, 4))
                if r.status_code != 200:
                    return []
                songs = (r.json().get("data") or {}).get("song") or {}
                songs = songs.get("list") or []
            except Exception as e:
                _log("qq/search:", repr(e))
                return []

            ranked = []
            for s in songs:
                score = 0.0
                songname = s.get("songname") or ""
                singer = [x.get("name", "") for x in (s.get("singer") or [])]
                rn, rt = _norm(clean_title(songname)), _norm(c_title)
                if any(_norm(a) == _norm(b) for a in c_artists for b in singer):
                    score += 45
                elif any(_ratio(_norm(a), _norm(b)) > 0.9
                         for a in c_artists for b in singer):
                    score += 12
                else:
                    score -= 25
                if rn == rt:
                    score += 30
                elif rn and (rn.startswith(rt) or rt.startswith(rn)):
                    score += 18
                else:
                    score += _ratio(rn, rt) * 20
                qd = int(s.get("interval") or 0) * 1000
                if duration_ms > 0 and qd > 0:
                    diff = abs(qd - duration_ms)
                    if diff > _DUR_TOLERANCE_MS:
                        continue   # 版本/时长对不上：直接跳过，避免 Live/翻唱
                    score += 20 if diff <= 1500 else -10
                if _VERSION_WORDS.search(songname or ""):
                    score -= 30
                if s.get("songmid") or s.get("mid"):
                    ranked.append((score, s))
            ranked.sort(key=lambda x: x[0], reverse=True)
            return ranked

        def fetch_lrc(mid: str):
            """拉 QQ 歌词（优先明文，失败退回 base64）。"""
            try:
                for extra in ("nobase64=1&uin=0", "uin=0"):
                    r = requests.get(
                        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                        params={"songmid": mid, "format": "json",
                                "nobase64": 1 if "nobase64" in extra else 0,
                                "uin": 0},
                        headers={"User-Agent": UA,
                                 "Referer": "https://y.qq.com/portal/player.html"},
                        timeout=(2, 4))
                    if r.status_code != 200:
                        continue
                    j = r.json()
                    code = j.get("code", j.get("retcode", -1))
                    if code != 0:
                        continue
                    lyric = j.get("lyric") or ""
                    if "nobase64" not in extra and lyric:
                        try:
                            import base64
                            lyric = base64.b64decode(lyric).decode("utf-8",
                                                                    "ignore")
                        except Exception:
                            continue
                    if lyric.strip():
                        return lyric
                return ""
            except Exception as e:
                _log("qq/lyric:", repr(e))
                return ""

        q1 = "{} {}".format(c_title, c_artists[0] if c_artists else "").strip()
        ranked = run_query(q1)
        if not ranked and c_artists:
            ranked = run_query(c_title)   # 带艺人搜不到时只按标题
        got = 0
        for score, s in ranked:
            if got >= 3:
                break
            mid = s.get("songmid") or s.get("mid")
            if not mid:
                continue
            lrc = fetch_lrc(mid)
            lines = self.parse_lrc(lrc)
            if not lines:
                continue
            cov = lines[-1].time_ms
            qd = int(s.get("interval") or 0) * 1000
            if qd > 0:
                score += 25 if cov > qd * 0.4 else -15
            out.append(_Candidate(score + 10, "QQ音乐·同步", lines))
            got += 1
        return out

    def candidate_info(self, key: str):
        """返回 (当前第几份(从1), 共几份, 标签)；无则 None。"""
        with self._lock:
            pool = self._pool.get(key)
        if not pool:
            return None
        cur = self._pick.get(key, 0) % len(pool)
        return cur + 1, len(pool), pool[cur].label

    @staticmethod
    def _text_sig(c: _Candidate) -> str:
        return "|".join(l.text for l in c.lines[:14])

    def cycle_next(self, key: str):
        """换到下一份“内容不同”且“完整可用”的候选。

        返回 dict {lines,label,index(0起),total,changed}；
        没有候选返回 None；没有可切换的（内容相同或其余都是残缺片段）
        时 changed=False。
        """
        with self._lock:
            pool = self._pool.get(key)
            if not pool:
                return None
            cur = self._pick.get(key, 0) % len(pool)
            total = len(pool)
            base_sig = self._text_sig(pool[cur])
            # 先找“内容不同且行数足够(≥8)”的
            full = [c for c in pool if len(c.lines) >= 8]
            for step in range(1, total + 1):
                idx = (cur + step) % total
                c = pool[idx]
                if self._text_sig(c) == base_sig:
                    continue
                if len(c.lines) >= 8 or not full:
                    self._pick[key] = idx
                    return {"lines": list(c.lines), "label": c.label,
                            "index": idx, "total": total, "changed": True}
            # 全池内容相同 或 其余都残缺
            c = pool[cur]
            other_full = [x for x in pool if x is not c and len(x.lines) >= 8]
            reason = "same" if not other_full and len(pool) > 1 \
                and len(set(self._text_sig(x) for x in pool)) == 1 \
                else "short_only"
            if len(pool) == 1:
                reason = "same"
            return {"lines": list(c.lines), "label": c.label,
                    "index": cur, "total": total, "changed": False,
                    "reason": reason}

    def invalidate(self, title: str, artist: str, duration_ms: int = 0):
        key = self.key(title, artist, duration_ms)
        with self._lock:
            self._pool.pop(key, None)
            self._pick.pop(key, None)

    def _score_record(self, rec, c_title, c_artists, duration_ms) -> float:
        score = 0.0
        rec_title = clean_title(rec.get("trackName") or "")
        rec_art = clean_artist(rec.get("artistName") or "")
        rn, rt = _norm(rec_title), _norm(c_title)

        if rec_art and c_artists:
            hit = any(_norm(a) == _norm(b) for a in c_artists for b in rec_art)
            hit2 = any(_ratio(_norm(a), _norm(b)) > 0.9
                       for a in c_artists for b in rec_art)
            score += 45 if hit else (15 if hit2 else -30)
        if rn:
            if rn == rt:
                score += 40
            elif rt.startswith(rn) or rn.startswith(rt):
                score += 25
            else:
                score += _ratio(rn, rt) * 30

        # 时长：Apple 有时长时，偏差太大说明是另一个版本（live/翻唱等）
        rec_dur_ms = int(rec.get("duration") or 0) * 1000
        if duration_ms > 0 and rec_dur_ms > 0:
            diff = abs(rec_dur_ms - duration_ms)
            if diff <= 1500:
                score += 25
            elif diff <= _DUR_TOLERANCE_MS:
                score -= 15
            else:
                score -= 120

        if rec.get("instrumental"):
            score -= 200
        return score

    @staticmethod
    def _clearly_other_song(rec, c_artists, duration_ms) -> bool:
        """艺人名与时长都对不上时，判定为“另一首歌/另一版本”，直接排除。"""
        rec_art = clean_artist(rec.get("artistName") or "")
        if not c_artists or not rec_art:
            return False   # 缺信息时不武断排除，交给打分
        hit = any(_norm(a) == _norm(b) for a in c_artists for b in rec_art)
        if hit:
            return False
        rec_dur_ms = int(rec.get("duration") or 0) * 1000
        if duration_ms > 0 and rec_dur_ms > 0:
            return abs(rec_dur_ms - duration_ms) > 1500
        return False

    # ------------------------------------------------------------------
    # LRCLib
    # ------------------------------------------------------------------
    def _lrclib_get(self, c_title, c_artists, duration_ms):
        try:
            params = {"track_name": c_title}
            if c_artists:
                params["artist_name"] = c_artists[0]
            if duration_ms > 0:
                params["duration"] = max(1, duration_ms // 1000)
            r = requests.get("https://lrclib.net/api/get", params=params,
                             headers={"User-Agent": UA}, timeout=(2, 4))
            if r.status_code == 200:
                rec = r.json()
                if rec:
                    return [(rec, "LRCLib精确")]
        except Exception as e:
            _log("lrclib/get:", repr(e))
        return []

    def _lrclib_search(self, c_title, c_artists):
        q = f"{c_title} {' '.join(c_artists[:2])}".strip()
        try:
            r = requests.get("https://lrclib.net/api/search",
                             params={"q": q}, headers={"User-Agent": UA}, timeout=(2, 4))
            if r.status_code == 200:
                out = []
                for rec in (r.json() or [])[:8]:
                    out.append((rec, "LRCLib"))
                return out
        except Exception as e:
            _log("lrclib/search:", repr(e))
        return []

    # ------------------------------------------------------------------
    # 网易云
    # ------------------------------------------------------------------
    def _netease(self, raw_title, c_title, c_artists, duration_ms):
        """返回已排序、已打分、歌词已取回的候选列表（含“仅标题”二次搜索）。"""

        def search_and_rank(query: str, limit: int):
            try:
                r = requests.post(
                    "https://music.163.com/api/search/get/web",
                    data={"s": query, "type": 1, "limit": limit},
                    headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                    timeout=(2, 4))
                if r.status_code != 200:
                    return []
                songs = (r.json().get("result") or {}).get("songs") or []
            except Exception as e:
                _log("netease/search:", repr(e))
                return []

            ranked = []
            for s in songs:
                score = 0.0
                s_art = [a.get("name", "") for a in (s.get("artists") or [])]
                rn = _norm(clean_title(s.get("name") or ""))
                rt = _norm(c_title)
                if any(_norm(a) == _norm(b) for a in c_artists for b in s_art):
                    score += 45
                elif any(_ratio(_norm(a), _norm(b)) > 0.9
                         for a in c_artists for b in s_art):
                    score += 12
                else:
                    score -= 25
                if rn == rt:
                    score += 30
                elif rn and (rn.startswith(rt) or rt.startswith(rn)):
                    score += 18
                else:
                    score += _ratio(rn, rt) * 20
                s_dur = int(s.get("duration") or 0)
                if duration_ms > 0 and s_dur > 0:
                    diff = abs(s_dur - duration_ms)
                    score += 20 if diff <= 1500 else (
                        -10 if diff <= _DUR_TOLERANCE_MS else -110)
                name = s.get("name", "")
                if _VERSION_WORDS.search(name or "") \
                        and _VERSION_WORDS.search(raw_title or ""):
                    pass  # Apple 标题也带版本词时不算降权
                elif _VERSION_WORDS.search(name or ""):
                    score -= 30  # 网易结果是 live/翻唱等版本
                ranked.append((score, s))
            ranked.sort(key=lambda x: x[0], reverse=True)
            return ranked

        def fetch_lyrics(ranked_list, max_take):
            out = []
            for score, s in ranked_list:
                if len(out) >= max_take:
                    break
                try:
                    lr = requests.get(
                        f"https://music.163.com/api/song/lyric?id={s['id']}"
                        f"&lv=1&kv=1&tv=-1",
                        headers={"User-Agent": UA,
                                 "Referer": "https://music.163.com"},
                        timeout=(2, 4))
                    if lr.status_code != 200:
                        continue
                    lrc = ((lr.json().get("lrc") or {}).get("lyric")) or ""
                    lines = self.parse_lrc(lrc)
                    if not lines:
                        continue
                    # 覆盖率加分：整段比片段更可信
                    cov = lines[-1].time_ms
                    s_dur = int(s.get("duration") or duration_ms or 0)
                    if s_dur > 0:
                        score += 25 if cov > s_dur * 0.4 else -15
                    out.append({"score": score, "syncedLyrics": lrc})
                except Exception as e:
                    _log("netease/lyric:", repr(e))
            return out

        q1 = "{} {}".format(c_title, c_artists[0] if c_artists else "").strip()
        ranked1 = search_and_rank(q1, 15)
        out = fetch_lyrics(ranked1, 3)
        if not out and c_artists:
            # 带艺人没搜到：只按标题再搜一次
            ranked2 = search_and_rank(c_title, 15)
            out = fetch_lyrics(ranked2, 3)
        return out

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    TAG_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
    META_RE = re.compile(r"^\[(ti|ar|al|by|re|ve|length|total|offset):", re.I)
    # 网易云等会把“作词/作曲/编曲/制作…”做成带时间戳的滚动制作信息行，
    # 这些不是演唱歌词，会占据行位并干扰扫光，全部过滤
    CREDIT_RE = re.compile(
        r"^(作词|作曲|编曲|制作人?|监制|混音|母带(工程)?|录音(师)?|和声(编写)?|"
        r"吉他|贝斯|鼓|键盘|钢琴|弦乐(编写)?|配唱(制作人)?|艺人统筹|企划(制作)?|"
        r"统筹|原唱|原曲|原词|出品|发行|OP|SP|词|曲)\s*[:：]",
        re.I)
    CREDIT_RE2 = re.compile(r"^[\u300c「【\[]?(作词|作曲|编曲|制作|监制|混音|录音|和声|企划|出品|OP|SP)[\s:：]")

    @classmethod
    def _is_credit(cls, text: str) -> bool:
        if len(text) > 40:
            return False
        return bool(cls.CREDIT_RE.match(text)) or bool(cls.CREDIT_RE2.match(text))

    @classmethod
    def _tag_to_ms(cls, minutes, seconds, frac) -> int:
        ms = int(minutes) * 60000 + int(seconds) * 1000
        if frac:
            digits = frac[:3]
            ms += int(digits) * (10 ** (3 - len(digits)))
        return ms

    @classmethod
    def parse_lrc(cls, lrc_text: str) -> list:
        if not lrc_text:
            return []
        offset = 0
        merged: dict[int, list] = {}
        for raw in lrc_text.splitlines():
            line = raw.strip()
            if not line:
                continue
            m = re.match(r"^\[offset:\s*([+-]?\d+)\s*\]", line)
            if m:
                offset = int(m.group(1))
                continue
            if cls.META_RE.match(line):
                continue
            tags = cls.TAG_RE.findall(line)
            if not tags:
                continue
            last = 0
            for m2 in cls.TAG_RE.finditer(line):
                last = m2.end()
            text = line[last:].strip()
            if not text:
                continue
            if cls._is_credit(text):
                continue
            for mm, ss, ff in tags:
                t = cls._tag_to_ms(mm, ss, ff) + offset
                if t >= 0:
                    merged.setdefault(t, []).append(text)
        result = sorted(
            (LyricLine(t, " / ".join(v)) for t, v in merged.items()),
            key=lambda x: x.time_ms)
        return result

    @classmethod
    def parse_plain(cls, text: str) -> list:
        lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
        return [LyricLine(i * 3000, l) for i, l in enumerate(lines)]
