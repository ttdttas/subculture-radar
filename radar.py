#!/usr/bin/env python3
"""
서브컬쳐 레이더 — 블루아카 / 원신 / 젠존제 / 스타레일 / 명조의
새 떡밥·공식 소식·화제글·많이 찾는 검색어를 모아 디스코드/텔레그램으로 보내는 수집기.

외부 패키지 없이 Python 3.11+ 기본 라이브러리만 사용합니다.

  python radar.py              # 한 번 수집하고 알림 전송
  python radar.py --dry-run    # 전송/상태저장 없이 결과만 출력 (테스트용)
  python radar.py --loop       # 켜두면 config.toml 의 schedule 시각마다 자동 실행

환경 변수 (모두 선택):
  DISCORD_WEBHOOK_URL                 디스코드 채널 웹훅 주소
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 텔레그램 봇 토큰 / 받을 채팅 ID
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET  Reddit 앱 키 (클라우드에서 Reddit 차단될 때)
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET    네이버 검색 API 키 (한국 뉴스 보강)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import math
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # 윈도우에 tzdata 가 없을 때
    KST = timezone(timedelta(hours=9), "KST")

log = logging.getLogger("radar")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
REDDIT_UA = "python:subculture-radar:v1.0 (personal news digest)"
ATOM = "{http://www.w3.org/2005/Atom}"
REDDIT_DELAY = 2.0
KOREAN_BONUS = 2.0
WEEKDAYS = "월화수목금토일"


# ════════════════════════════════════════════
#  HTTP
# ════════════════════════════════════════════
def http_get(url: str, headers: dict | None = None, data: bytes | None = None,
             timeout: int = 20, retries: int = 2) -> bytes | None:
    """GET(또는 data 가 있으면 POST). 실패하면 None — 한 소스가 죽어도 전체는 계속 돈다."""
    hdrs = {"User-Agent": BROWSER_UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s ← %s", e.code, url[:120])
            if e.code in (400, 401, 403, 404):
                return None
            if e.code == 429 and attempt < retries:   # 속도 제한 → 길게 쉬고 재시도
                time.sleep(6 * (attempt + 1))
                continue
        except Exception as e:  # 네트워크 오류, 타임아웃 등
            log.warning("요청 실패 (%s) ← %s", type(e).__name__, url[:120])
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None


def http_post_json(url: str, payload: dict, headers: dict | None = None) -> bool:
    hdrs = {"Content-Type": "application/json", "User-Agent": BROWSER_UA}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # 디스코드 속도 제한
                time.sleep(2 + attempt * 2)
                continue
            log.error("전송 실패 HTTP %s: %s", e.code, e.read()[:300])
            return False
        except Exception as e:
            log.error("전송 실패 (%s)", e)
            time.sleep(2)
    return False


# ════════════════════════════════════════════
#  데이터 구조 / 유틸
# ════════════════════════════════════════════
@dataclass
class Item:
    game: str
    title: str
    url: str
    source: str
    kind: str = "news"            # news | reddit | trend | video
    published: datetime | None = None
    upvotes: int = 0
    comments: int = 0
    flair: str = ""
    traffic: str = ""             # 구글 트렌드 검색량 (예: "2만+")
    rank: int = -1                # 점수 정보가 없을 때 인기순 순위 (0 = 1위)
    tags: set = field(default_factory=set)
    score: float = 0.0
    outlets: set = field(default_factory=set)   # 같은 소식을 다룬 매체들

    def __post_init__(self):
        self.outlets.add(self.source)

    @property
    def coverage(self) -> int:
        return len(self.outlets)

    @property
    def key(self) -> str:
        return hashlib.sha1(norm_title(self.title).encode("utf-8")).hexdigest()[:16]


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)       # [속보], (종합) 같은 머리말 제거
    t = re.sub(r"[^\w가-힣]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def bigrams(t: str) -> set[str]:
    # 한국어는 띄어쓰기가 매체마다 달라서(특별 방송/특별방송) 글자 2개 단위로 비교
    s = norm_title(t).replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(a: str, b: str, threshold: float = 0.55) -> bool:
    ta, tb = bigrams(a), bigrams(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def child_text(el: ET.Element, tag: str) -> str:
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_feed(data: bytes | None) -> list[dict]:
    """RSS 2.0 과 Atom 을 모두 읽는다."""
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        out.append({
            "title": strip_tags(child_text(it, "title")),
            "link": child_text(it, "link"),
            "date": parse_date(child_text(it, "pubDate")),
            "source": child_text(it, "source"),
            "el": it,
        })
    for e in root.iter(ATOM + "entry"):
        link_el = e.find(ATOM + "link")
        out.append({
            "title": strip_tags(child_text(e, ATOM + "title")),
            "link": link_el.get("href", "") if link_el is not None else "",
            "date": parse_date(child_text(e, ATOM + "published") or child_text(e, ATOM + "updated")),
            "source": "",
            "el": e,
        })
    return out


def ago(d: datetime | None, now: datetime) -> str:
    if not d:
        return ""
    mins = int((now - d).total_seconds() // 60)
    if mins < 1:
        return "방금"
    if mins < 60:
        return f"{mins}분 전"
    if mins < 60 * 24:
        return f"{mins // 60}시간 전"
    return f"{mins // (60 * 24)}일 전"


def short_num(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def slot_name(now: datetime) -> str:
    h = now.hour
    if 4 <= h < 10:
        return "아침"
    if 10 <= h < 16:
        return "점심"
    return "저녁"


# ════════════════════════════════════════════
#  수집기들
# ════════════════════════════════════════════
def fetch_google_news(game: dict, query: str, lang: str, since: datetime) -> list[Item]:
    if lang == "ko":
        params = {"q": f"{query} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    else:
        params = {"q": f"{query} when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    items = []
    for e in parse_feed(http_get(url))[:25]:
        if e["date"] and e["date"] < since:
            continue
        title, src = e["title"], e["source"]
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        items.append(Item(game["id"], title, e["link"], src or "뉴스", "news", e["date"]))
    return items


def fetch_naver_news(game: dict, query: str, since: datetime) -> list[Item]:
    cid, secret = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        return []
    url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
        {"query": query, "display": 20, "sort": "date"})
    raw = http_get(url, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret})
    if not raw:
        return []
    items = []
    for it in json.loads(raw).get("items", []):
        d = parse_date(it.get("pubDate"))
        if d and d < since:
            continue
        items.append(Item(game["id"], strip_tags(it.get("title", "")),
                          it.get("originallink") or it.get("link", ""), "네이버 뉴스", "news", d))
    return items


_reddit_token: str | None = None
_reddit_json_blocked = False


def reddit_token() -> str | None:
    """Reddit 앱 키가 있으면 OAuth 토큰을 받는다 (GitHub Actions 등 클라우드에서 차단 회피)."""
    global _reddit_token
    if _reddit_token is not None:
        return _reddit_token or None
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    _reddit_token = ""
    if cid and secret:
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        raw = http_get("https://www.reddit.com/api/v1/access_token",
                       {"Authorization": f"Basic {auth}", "User-Agent": REDDIT_UA,
                        "Content-Type": "application/x-www-form-urlencoded"},
                       data=b"grant_type=client_credentials")
        if raw:
            _reddit_token = json.loads(raw).get("access_token", "") or ""
    return _reddit_token or None


def fetch_reddit(game: dict, sub: str, since: datetime) -> list[Item]:
    global _reddit_json_blocked
    time.sleep(REDDIT_DELAY)          # Reddit 은 연속 요청을 싫어함
    token = reddit_token()
    if token:
        url = f"https://oauth.reddit.com/r/{sub}/hot?limit=40&raw_json=1"
        hdrs = {"Authorization": f"Bearer {token}", "User-Agent": REDDIT_UA}
    else:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=40&raw_json=1"
        hdrs = {"User-Agent": REDDIT_UA}
    raw = None if (_reddit_json_blocked and not token) else http_get(url, hdrs)
    if raw is None and not token:
        _reddit_json_blocked = True
    items: list[Item] = []
    if raw:
        try:
            children = json.loads(raw)["data"]["children"]
        except Exception:
            children = []
        for c in children:
            p = c.get("data", {})
            if p.get("stickied"):
                continue
            d = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
            if d < since - timedelta(hours=10):   # 핫 게시판은 하루 정도까지 허용
                continue
            items.append(Item(game["id"], p.get("title", ""),
                              "https://www.reddit.com" + p.get("permalink", ""),
                              f"r/{sub}", "reddit", d,
                              upvotes=int(p.get("ups", 0)), comments=int(p.get("num_comments", 0)),
                              flair=(p.get("link_flair_text") or "").strip()))
        return items
    time.sleep(REDDIT_DELAY)
    # JSON 이 막히면 RSS 로 재시도 (점수 정보는 없음)
    for i, e in enumerate(parse_feed(http_get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day",
                                              {"User-Agent": REDDIT_UA}))[:20]):
        items.append(Item(game["id"], e["title"], e["link"], f"r/{sub}", "reddit", e["date"], rank=i))
    return items


def fetch_youtube(game: dict, channel_id: str, since: datetime) -> list[Item]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    items = []
    for e in parse_feed(http_get(url))[:10]:
        if e["date"] and e["date"] < since:
            continue
        items.append(Item(game["id"], e["title"], e["link"], "YouTube 공식", "video", e["date"],
                          tags={"official"}))
    return items


def fetch_suggestions(seed: str, hl: str) -> list[str]:
    """구글 자동완성 = 사람들이 실제로 많이 검색하는 조합."""
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "hl": hl, "ie": "utf-8", "oe": "utf-8", "q": seed})
    raw = http_get(url, retries=1)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return [s for s in data[1] if s.strip().lower() != seed.strip().lower()]
    except Exception:
        return []


def fetch_trends(geo: str) -> list[dict]:
    """구글 트렌드 급상승 검색어 RSS."""
    raw = http_get(f"https://trends.google.com/trending/rss?geo={geo}")
    out = []
    for e in parse_feed(raw):
        traffic, news_titles, news_url = "", [], ""
        for ch in e["el"].iter():
            tag = ch.tag.split("}")[-1]
            if tag == "approx_traffic":
                traffic = (ch.text or "").strip()
            elif tag == "news_item_title":
                news_titles.append(strip_tags(ch.text or ""))
            elif tag == "news_item_url" and not news_url:
                news_url = (ch.text or "").strip()
        out.append({"title": e["title"], "traffic": traffic, "news": news_titles,
                    "url": news_url, "date": e["date"], "geo": geo})
    return out


def match_trends(game: dict, trends: list[dict]) -> list[Item]:
    items = []
    for t in trends:
        hay = " ".join([t["title"], *t["news"]]).lower()
        if any(a.lower() in hay for a in game["aliases"]):
            url = t["url"] or "https://trends.google.com/trending?geo=" + t["geo"]
            items.append(Item(game["id"], f"'{t['title']}' 급상승 검색어 ({t['geo']})", url,
                              "Google 트렌드", "trend", t["date"], traffic=t["traffic"], tags={"trend"}))
    return items


# ════════════════════════════════════════════
#  분류 / 점수 / 중복 제거
# ════════════════════════════════════════════
def compile_keywords(kw: dict) -> dict[str, re.Pattern]:
    out = {}
    for cat, words in kw.items():
        parts = []
        for w in words:
            esc = re.escape(w.lower())
            if w.isascii():   # 영문은 단어 경계로 (prevent 안의 event 같은 오탐 방지)
                esc = (r"\b" if w[0].isalnum() else "") + esc + (r"\b" if w[-1].isalnum() else "")
            parts.append(esc)
        out[cat] = re.compile("|".join(parts), re.IGNORECASE)
    return out


LEAK_FLAIRS = {"reliable": "높음", "datamined": "높음", "official": None,
               "questionable": "보통", "rumor": "낮음", "unreliable": "낮음", "fake": "낮음"}


def classify(item: Item, pats: dict[str, re.Pattern]) -> None:
    text = item.title
    flair = item.flair.lower()
    if pats["leak"].search(text) or "leak" in item.source.lower():
        item.tags.add("leak")
    if pats["official"].search(text) or "official" in flair or "news" in flair:
        item.tags.add("official")
    if pats["hot"].search(text):
        item.tags.add("hot")
    if "official" in flair:          # 유출 게시판의 '공식' 플레어는 공식으로
        item.tags.discard("leak")


def reliability(item: Item) -> str:
    for k, v in LEAK_FLAIRS.items():
        if k in item.flair.lower() and v:
            return v
    return "확인 안 됨"


def score(item: Item, now: datetime) -> float:
    s = 1.0
    if item.kind == "reddit" and item.rank >= 0 and not item.upvotes:
        s += max(0.0, 5.0 - 0.25 * item.rank)          # RSS: 하루 인기순 위쪽일수록 가산
    elif item.kind == "reddit":
        s += 2 * math.log10(1 + item.upvotes) + math.log10(1 + item.comments)
    elif item.kind == "trend":
        s += 8
    else:
        s += 1.2 * (item.coverage - 1)
    s += 1.5 * ("leak" in item.tags) + 1.0 * ("official" in item.tags) + 1.2 * ("hot" in item.tags)
    if item.published and now - item.published < timedelta(hours=6):
        s += 1.0
    if item.kind == "news" and re.search(r"[가-힣]", item.title):
        s += KOREAN_BONUS                               # 국내 기사 우선
    return round(s, 2)


def merge_duplicates(items: list[Item]) -> list[Item]:
    """제목이 비슷한 기사는 하나로 합치고, 다룬 매체 수(coverage)를 올린다."""
    merged: list[Item] = []
    for it in items:
        for m in merged:
            if m.kind == it.kind == "news" and (m.url == it.url or similar(m.title, it.title)):
                m.outlets.add(it.source)
                break
            if m.key == it.key:
                break
        else:
            merged.append(it)
    return merged


def primary_section(item: Item) -> str:
    if "trend" in item.tags:
        return "hot"
    if "leak" in item.tags:
        return "leak"
    if "official" in item.tags:
        return "official"
    return "hot"


# ════════════════════════════════════════════
#  상태 저장 (이미 보낸 소식 / 지난번 검색어)
# ════════════════════════════════════════════
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


# ════════════════════════════════════════════
#  수집 전체 흐름
# ════════════════════════════════════════════
def collect(cfg: dict, state_dir: Path, now: datetime) -> dict:
    st = cfg["settings"]
    since = now - timedelta(hours=st.get("lookback_hours", 14))
    pats = compile_keywords(cfg["keywords"])
    seen: dict[str, str] = load_json(state_dir / "seen.json", {})
    prev_sugg: dict[str, list[str]] = load_json(state_dir / "suggest.json", {})

    trends = []
    for geo in st.get("trend_geos", ["KR"]):
        trends += fetch_trends(geo)
    log.info("구글 트렌드 %d개 확인", len(trends))

    report = {"now": now, "games": [], "new_count": 0, "seen": seen, "suggest": {}}
    for g in cfg["games"]:
        raw: list[Item] = []
        for q in g.get("news_ko", []):
            raw += fetch_google_news(g, q, "ko", since)
            raw += fetch_naver_news(g, q, since)
        for q in g.get("news_en", []):
            raw += fetch_google_news(g, q, "en", since)
        for sub in g.get("subreddits", []):
            raw += fetch_reddit(g, sub, since)
        for ch in g.get("youtube_channels", []):
            raw += fetch_youtube(g, ch, since)
        raw += match_trends(g, trends)

        aliases = [a.lower() for a in g["aliases"]]
        raw = [it for it in raw
               if not (it.kind == "news" and not any(a in it.title.lower() for a in aliases))  # 게임 이름 없는 기사 제외
               and not (it.kind != "trend" and "exclude" in pats and pats["exclude"].search(it.title + " " + it.flair))]
        items = merge_duplicates(raw)
        for it in items:
            classify(it, pats)
            it.score = score(it, now)
        fresh = [it for it in items if it.key not in seen]
        for it in fresh:
            seen[it.key] = now.isoformat()

        sections = {"official": [], "leak": [], "hot": []}
        for it in sorted(fresh, key=lambda x: -x.score):
            sec = sections[primary_section(it)]
            if len(sec) < st.get("max_per_section", 3):
                sec.append(it)

        # 자동완성 검색어
        sugg: list[str] = []
        for seed in g.get("suggest_ko", []):
            sugg += fetch_suggestions(seed, "ko")
        for seed in g.get("suggest_en", []):
            sugg += fetch_suggestions(seed, "en")
        sugg = list(dict.fromkeys(s.strip() for s in sugg if s.strip()))
        old = set(prev_sugg.get(g["id"], []))
        new_sugg = [s for s in sugg if s not in old] if old else []
        report["suggest"][g["id"]] = sugg[:30] if sugg else prev_sugg.get(g["id"], [])

        log.info("%s: 수집 %d → 새 소식 %d", g["name"], len(items), len(fresh))
        report["new_count"] += len(fresh)
        report["games"].append({"cfg": g, "sections": sections, "suggest": sugg[:8],
                                "new_suggest": new_sugg[:6], "fresh": len(fresh)})

    # 오래된 기록 정리
    cutoff = now - timedelta(days=st.get("seen_retention_days", 7))
    report["seen"] = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= cutoff}
    return report


# ════════════════════════════════════════════
#  출력 (마크다운 / 디스코드 / 텔레그램)
# ════════════════════════════════════════════
SECTION_TITLES = {"official": "📢 공식", "leak": "🕵️ 떡밥·유출", "hot": "🔥 화제"}


def fmt_item(it: Item, now: datetime, style: str) -> str:
    title = it.title if len(it.title) <= 90 else it.title[:88] + "…"
    meta = [it.source]
    if it.kind == "reddit" and it.upvotes:
        meta.append(f"⬆️{short_num(it.upvotes)} 💬{short_num(it.comments)}")
    if it.traffic:
        meta.append(f"검색 {it.traffic}")
    if it.coverage > 1:
        meta.append(f"매체 {it.coverage}곳")
    if a := ago(it.published, now):
        meta.append(a)
    badge = ""
    if "leak" in it.tags and "trend" not in it.tags:
        badge = f"[유출·신뢰도 {reliability(it)}] "
    meta_s = " · ".join(meta)
    if style == "html":
        return (f"• {html.escape(badge)}<a href=\"{html.escape(it.url, quote=True)}\">"
                f"{html.escape(title)}</a> <i>{html.escape(meta_s)}</i>")
    title_md = title.replace("[", "(").replace("]", ")")
    return f"- {badge}[{title_md}]({it.url}) · _{meta_s}_"


def game_block(gd: dict, now: datetime, style: str) -> str:
    lines = []
    b = (lambda s: f"<b>{html.escape(s)}</b>") if style == "html" else (lambda s: f"**{s}**")
    if gd["fresh"] == 0:
        lines.append("새 소식 없음")
    for sec, title in SECTION_TITLES.items():
        its = gd["sections"][sec]
        if its:
            lines.append(b(title))
            lines += [fmt_item(it, now, style) for it in its]
    esc = html.escape if style == "html" else (lambda s: s)
    if gd["suggest"]:
        lines.append(f"{b('🔍 많이 찾는 검색어')} {esc(', '.join(gd['suggest']))}")
    if gd["new_suggest"]:
        lines.append(f"{b('🆕 새로 뜬 검색어')} {esc(', '.join(gd['new_suggest']))}")
    return "\n".join(lines)


def top3(report: dict) -> list[tuple[dict, Item]]:
    allits = [(gd["cfg"], it) for gd in report["games"]
              for sec in gd["sections"].values() for it in sec]
    return sorted(allits, key=lambda x: -x[1].score)[:3]


def header(report: dict) -> str:
    now = report["now"]
    return (f"🎮 서브컬쳐 떡밥 레이더 — {now:%Y-%m-%d} ({WEEKDAYS[now.weekday()]}) "
            f"{slot_name(now)} · {now:%H:%M} KST · 새 소식 {report['new_count']}개")


def render_markdown(report: dict) -> str:
    now = report["now"]
    out = [f"# {header(report)}", ""]
    t = top3(report)
    if t:
        out.append("## 🏆 TOP 3")
        for i, (g, it) in enumerate(t, 1):
            out.append(f"{i}. {g['emoji']} **{g['name']}** " + fmt_item(it, now, "md")[2:])
        out.append("")
    for gd in report["games"]:
        g = gd["cfg"]
        out += [f"## {g['emoji']} {g['name']}", game_block(gd, now, "md"), ""]
    return "\n".join(out)


def send_discord(report: dict, webhook: str) -> None:
    now = report["now"]
    t = top3(report)
    top_txt = "\n".join(f"{i}. {g['emoji']} " + fmt_item(it, now, "md")[2:]
                        for i, (g, it) in enumerate(t, 1)) or "이번 회차엔 새 소식이 없어요."
    http_post_json(webhook, {"username": "서브컬쳐 레이더",
                             "embeds": [{"title": header(report), "description": top_txt[:4000],
                                         "color": 0xFF5C8A}]})
    for gd in report["games"]:
        g = gd["cfg"]
        if gd["fresh"] == 0 and not gd["new_suggest"]:
            continue
        http_post_json(webhook, {"username": "서브컬쳐 레이더",
                                 "embeds": [{"title": f"{g['emoji']} {g['name']}",
                                             "description": game_block(gd, now, "md")[:4000],
                                             "color": int(g.get("color", "5865F2"), 16)}]})
        time.sleep(0.7)


def send_telegram(report: dict, token: str, chat_id: str) -> None:
    now = report["now"]
    parts = [f"<b>{html.escape(header(report))}</b>"]
    t = top3(report)
    if t:
        parts.append("<b>🏆 TOP 3</b>\n" + "\n".join(
            f"{i}. {g['emoji']} " + fmt_item(it, now, "html")[2:] for i, (g, it) in enumerate(t, 1)))
    for gd in report["games"]:
        g = gd["cfg"]
        parts.append(f"<b>{g['emoji']} {html.escape(g['name'])}</b>\n" + game_block(gd, now, "html"))
    # 텔레그램 메시지 4096자 제한 → 나눠 보내기
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur += p + "\n\n"
    if cur.strip():
        chunks.append(cur)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for c in chunks:
        http_post_json(url, {"chat_id": chat_id, "text": c[:4096], "parse_mode": "HTML",
                             "disable_web_page_preview": True})
        time.sleep(0.5)


# ════════════════════════════════════════════
#  실행
# ════════════════════════════════════════════
def run_once(cfg: dict, base: Path, dry_run: bool = False) -> str:
    now = datetime.now(KST)
    state_dir = base / "state"
    report = collect(cfg, state_dir, now)
    md = render_markdown(report)

    rep_dir = base / "reports" / f"{now:%Y}" / f"{now:%m}"
    if not dry_run:
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / f"{now:%d}-{now:%H%M}.md").write_text(md, "utf-8")
        (base / "reports" / "latest.md").write_text(md, "utf-8")
        save_json(state_dir / "seen.json", report["seen"])
        save_json(state_dir / "suggest.json", report["suggest"])

        if hook := os.getenv("DISCORD_WEBHOOK_URL"):
            send_discord(report, hook)
            log.info("디스코드 전송 완료")
        tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if tg_token and tg_chat:
            send_telegram(report, tg_token, tg_chat)
            log.info("텔레그램 전송 완료")
    return md


def next_run(schedule: list[str], now: datetime) -> datetime:
    cands = []
    for day in (0, 1):
        for hm in schedule:
            h, m = map(int, hm.split(":"))
            t = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                cands.append(t)
    return min(cands)


def loop(cfg: dict, base: Path) -> None:
    schedule = cfg["settings"].get("schedule", ["07:50", "11:50", "18:50"])
    log.info("상시 실행 모드 — 매일 %s (KST)", ", ".join(schedule))
    while True:
        nxt = next_run(schedule, datetime.now(KST))
        log.info("다음 실행: %s", nxt.strftime("%m-%d %H:%M"))
        while (remain := (nxt - datetime.now(KST)).total_seconds()) > 0:
            time.sleep(min(remain, 300))
        try:
            run_once(cfg, base)
        except Exception:
            log.exception("수집 중 오류 — 다음 회차에 다시 시도합니다")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="서브컬쳐 게임 떡밥 레이더")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--dry-run", action="store_true", help="전송·저장 없이 결과만 출력")
    ap.add_argument("--loop", action="store_true", help="schedule 시각마다 계속 실행")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = tomllib.loads(Path(args.config).read_text("utf-8"))
    base = Path(args.config).resolve().parent

    if args.loop:
        loop(cfg, base)
        return 0
    print(run_once(cfg, base, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
