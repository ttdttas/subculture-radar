#!/usr/bin/env python3
"""
서브컬쳐 레이더 — 블루아카 / 원신 / 젠존제 / 스타레일 / 명조의
새 떡밥·공식 소식·커뮤니티 화제글·많이 찾는 검색어를 모으는 수집기.

외부 패키지 없이 Python 3.11+ 기본 라이브러리만 사용합니다.

  python radar.py                  # 한 번 수집 (알림 시각이면 알림도 전송)
  python radar.py --notify-now     # 지금 바로 알림까지 보내기
  python radar.py --dry-run        # 저장·전송 없이 결과만 화면에 출력
  python radar.py --loop           # 켜 두면 interval_minutes 마다 계속 수집
  python radar.py --data-dir DIR   # 상태·사이트 데이터를 둘 폴더 (기본: 이 폴더)

수집 출처
  뉴스      구글 뉴스(한/영), 네이버 뉴스 API(선택)
  커뮤니티  디시인사이드 개념글, 아카라이브 베스트, Reddit(접속될 때만)
  검색어    구글·네이버·유튜브 자동완성, 구글 트렌드 급상승, 나무위키 인기 검색어

환경 변수 (모두 선택):
  DISCORD_WEBHOOK_URL                      디스코드 채널 웹훅 주소
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID     텔레그램 봇 토큰 / 받을 채팅 ID
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   승인받은 Reddit 앱 키가 있을 때
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET     네이버 검색 API 키 (국내 기사 보강)
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
WEEKDAYS = "월화수목금토일"

# 조절용 상수 (테스트에서 0 으로 바꿈)
REDDIT_DELAY = 2.0
COMMUNITY_DELAY = 1.0
TRANSLATE_DELAY = 0.3
SUGGEST_DELAY = 0.2
KOREAN_BONUS = 2.0
PREVIEW_MAX = 25             # 한 회차에 본문 미리보기를 가져올 글 수
EXCERPT_LEN = 180            # 미리보기 글자 수 (원문 재배포가 되지 않게 짧게)
DEADLINE = 0.0               # 이 시각(time.time)이 지나면 남은 요청은 다음 회차로
MAX_NEW_PER_KIND = {"news": 60, "community": 30, "reddit": 20, "trend": 10, "video": 10}  # 한 번에 저장할 게임별 새 소식 수
TRANSLATE_BACKFILL = 40      # 한 회차에 추가로 번역할 이전 소식 수


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
            if e.code == 429 and attempt < retries:
                time.sleep(6 * (attempt + 1))
                continue
        except Exception as e:
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
            if e.code == 429:
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
    kind: str = "news"            # news | reddit | community | trend | video
    published: datetime | None = None
    upvotes: int = 0
    comments: int = 0
    views: int = 0
    flair: str = ""               # 말머리 / 플레어 / 탭
    traffic: str = ""             # 구글 트렌드 검색량
    rank: int = -1                # 점수 정보가 없을 때 인기순 순위 (0 = 1위)
    title_ko: str = ""            # 자동 번역 제목 (원래 한국어면 비어 있음)
    excerpt: str = ""             # 본문 첫머리 미리보기 (짧게)
    tags: set = field(default_factory=set)
    score: float = 0.0
    outlets: set = field(default_factory=set)

    def __post_init__(self):
        self.outlets.add(self.source)

    @property
    def coverage(self) -> int:
        return len(self.outlets)

    @property
    def key(self) -> str:
        return item_key(self.game, self.kind, self.title, self.url)


def item_key(game: str, kind: str, title: str, url: str) -> str:
    """같은 글인지 판단하는 키. 뉴스는 매체가 달라도 같은 소식이면 하나로(제목 기준),
    커뮤니티 글은 '가슴', 'ㅋㅋ' 같은 짧은 제목이 겹치기 쉬워서 주소 기준. 게임별로 따로."""
    basis = "t:" + norm_title(title) if kind in ("news", "trend", "video") else "u:" + url
    return hashlib.sha1(f"{game}|{basis}".encode("utf-8")).hexdigest()[:16]


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
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
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def to_int(s: str | None) -> int:
    try:
        return int(re.sub(r"[^\d]", "", s or "") or 0)
    except ValueError:
        return 0


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
        out.append({"title": strip_tags(child_text(it, "title")), "link": child_text(it, "link"),
                    "date": parse_date(child_text(it, "pubDate")), "source": child_text(it, "source"), "el": it})
    for e in root.iter(ATOM + "entry"):
        link_el = e.find(ATOM + "link")
        out.append({"title": strip_tags(child_text(e, ATOM + "title")),
                    "content": child_text(e, ATOM + "content"),
                    "link": link_el.get("href", "") if link_el is not None else "",
                    "date": parse_date(child_text(e, ATOM + "published") or child_text(e, ATOM + "updated")),
                    "source": "", "el": e})
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


def alias_pattern(aliases: list[str]) -> re.Pattern:
    """게임 이름 매칭. 한글 이름은 앞 글자가 한글이면 제외 (예: '복원신청' 안의 '원신')."""
    parts = []
    for a in aliases:
        esc = re.escape(a)
        parts.append(rf"\b{esc}\b" if a.isascii() else rf"(?<![가-힣]){esc}")
    return re.compile("|".join(parts), re.IGNORECASE)


# ════════════════════════════════════════════
#  수집기 — 뉴스
# ════════════════════════════════════════════
def fetch_google_news(game: dict, query: str, lang: str, since: datetime) -> list[Item]:
    if lang == "ko":
        params = {"q": f"{query} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    else:
        params = {"q": f"{query} when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
    if not site_ok("구글 뉴스"):
        return []
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)
    raw = http_get(url, retries=1, timeout=15)
    site_result("구글 뉴스", raw is not None)
    items = []
    for e in parse_feed(raw)[:25]:
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


def fetch_youtube(game: dict, channel_id: str, since: datetime) -> list[Item]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    items = []
    for e in parse_feed(http_get(url))[:10]:
        if e["date"] and e["date"] < since:
            continue
        items.append(Item(game["id"], e["title"], e["link"], "YouTube 공식", "video", e["date"],
                          tags={"official"}))
    return items


# ════════════════════════════════════════════
#  수집기 — 커뮤니티 (디시 / 아카라이브 / Reddit)
# ════════════════════════════════════════════
_blocked: dict[str, int] = {}   # 사이트별 연속 실패 수 — 2번 막히면 이번 회차는 건너뜀


def past_deadline() -> bool:
    return bool(DEADLINE) and time.time() > DEADLINE


def site_ok(site: str) -> bool:
    return _blocked.get(site, 0) < 2 and not past_deadline()


def site_result(site: str, ok: bool) -> None:
    if ok:
        _blocked[site] = 0
    else:
        _blocked[site] = _blocked.get(site, 0) + 1
        if _blocked[site] == 2:
            log.warning("%s 이(가) 접속을 막고 있어 이번 회차는 건너뜁니다", site)


def parse_dcinside(raw: str, gid: str, since: datetime) -> list[dict]:
    """디시 갤러리 목록 HTML → 글 목록 (공지·설문·광고 제외)."""
    out = []
    for no, typ, body in re.findall(
            r'<tr class="ub-content us-post[^"]*" data-no="(\d+)" data-type="([^"]*)"(.*?)</tr>', raw, re.S):
        if "notice" in typ:
            continue
        subj = re.search(r'class="gall_subject">(.*?)</td>', body, re.S)
        subj = strip_tags(subj.group(1)) if subj else ""
        if subj in ("공지", "설문", "AD", "이슈"):
            continue
        t = re.search(r'class="gall_tit[^"]*">\s*<a [^>]*>(.*?)</a>', body, re.S)
        if not t:
            continue
        d = re.search(r'class="gall_date" title="([^"]+)"', body)
        dt = None
        if d:
            try:
                dt = datetime.strptime(d.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                dt = None
        if dt and dt < since:
            continue
        rep = re.search(r'reply_num">\[(\d+)', body)
        out.append({
            "title": strip_tags(t.group(1)), "flair": subj, "published": dt,
            "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gid}&no={no}",
            "views": to_int((re.search(r'class="gall_count">([^<]*)<', body) or [None, ""])[1]),
            "upvotes": to_int((re.search(r'class="gall_recommend">([^<]*)<', body) or [None, ""])[1]),
            "comments": int(rep.group(1)) if rep else 0,
        })
    return out


def fetch_dcinside(game: dict, gid: str, since: datetime) -> list[Item]:
    if not site_ok("디시인사이드"):
        return []
    time.sleep(COMMUNITY_DELAY)
    url = f"https://gall.dcinside.com/mgallery/board/lists/?id={gid}&exception_mode=recommend"
    raw = http_get(url, {"Referer": "https://gall.dcinside.com/"}, retries=1)
    site_result("디시인사이드", raw is not None)
    if not raw:
        return []
    return [Item(game["id"], p["title"], p["url"], "디시 개념글", "community", p["published"],
                 upvotes=p["upvotes"], comments=p["comments"], views=p["views"], flair=p["flair"])
            for p in parse_dcinside(raw.decode("utf-8", "replace"), gid, since)]


def parse_arca(raw: str, slug: str, since: datetime) -> list[dict]:
    """아카라이브 채널 목록 HTML → 글 목록 (공지 제외)."""
    out = []
    chunks = re.split(r'(?=<a class="vrow column)', raw)
    for ch in chunks[1:]:
        head = re.match(r'<a class="vrow column([^"]*)" href="([^"]+)"', ch)
        if not head or "notice" in head.group(1):
            continue
        ch = ch.split("</a>\n", 1)[0] if "vrow-preview" not in ch else ch.split('class="vrow-preview"', 1)[0]
        title = re.search(r'<span class="title">(.*?)</span>\s*(?:<span class="info">|</span>)', ch, re.S)
        if not title:
            continue
        badge = re.search(r'<span class="badge[^"]*">(.*?)</span>', ch, re.S)
        tm = re.search(r'<time datetime="([^"]+)"', ch)
        dt = parse_date(tm.group(1)) if tm else None
        if dt and dt < since:
            continue
        cmt = re.search(r'class="comment-count">\[?(\d+)', ch)
        href = html.unescape(head.group(2)).split("?")[0]
        out.append({
            "title": strip_tags(title.group(1)), "flair": strip_tags(badge.group(1)) if badge else "",
            "published": dt, "url": "https://arca.live" + href,
            "views": to_int((re.search(r'col-view">([^<]*)<', ch) or [None, ""])[1]),
            "upvotes": to_int((re.search(r'col-rate[^"]*">([^<]*)<', ch) or [None, ""])[1]),
            "comments": int(cmt.group(1)) if cmt else 0,
        })
    return out


def fetch_arca(game: dict, slug: str, since: datetime) -> list[Item]:
    if not site_ok("아카라이브"):
        return []
    time.sleep(COMMUNITY_DELAY)
    raw = http_get(f"https://arca.live/b/{slug}?mode=best", retries=1)
    site_result("아카라이브", raw is not None)
    if not raw:
        return []
    return [Item(game["id"], p["title"], p["url"], "아카 베스트", "community", p["published"],
                 upvotes=p["upvotes"], comments=p["comments"], views=p["views"], flair=p["flair"])
            for p in parse_arca(raw.decode("utf-8", "replace"), slug, since)]


_reddit_token: str | None = None


def reddit_token() -> str | None:
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
    if not site_ok("Reddit"):
        return []
    time.sleep(REDDIT_DELAY)
    token = reddit_token()
    items: list[Item] = []
    if token:
        raw = http_get(f"https://oauth.reddit.com/r/{sub}/hot?limit=40&raw_json=1",
                       {"Authorization": f"Bearer {token}", "User-Agent": REDDIT_UA})
        if raw:
            for c in json.loads(raw).get("data", {}).get("children", []):
                p = c.get("data", {})
                if p.get("stickied"):
                    continue
                d = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
                if d < since:
                    continue
                items.append(Item(game["id"], p.get("title", ""), "https://www.reddit.com" + p.get("permalink", ""),
                                  f"r/{sub}", "reddit", d, upvotes=int(p.get("ups", 0)),
                                  comments=int(p.get("num_comments", 0)),
                                  flair=(p.get("link_flair_text") or "").strip()))
            site_result("Reddit", True)
            return items
    rss = http_get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day", {"User-Agent": REDDIT_UA}, retries=1)
    site_result("Reddit", rss is not None)
    for i, e in enumerate(parse_feed(rss)[:20]):
        items.append(Item(game["id"], e["title"], e["link"], f"r/{sub}", "reddit", e["date"], rank=i,
                          excerpt=make_excerpt(e.get("content", ""))))
    return items


def make_excerpt(html_text: str) -> str:
    """HTML 본문 → 짧은 미리보기 글. 스크립트·템플릿·링크 문구 제거."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text or "", flags=re.S | re.I)
    t = re.sub(r"\{\{.*?\}\}", " ", t, flags=re.S)
    t = re.sub(r"<br\s*/?>|</p>|</div>", " ", t, flags=re.I)
    t = strip_tags(t)
    t = re.sub(r"- dc official App|추천검색\s*새로고침|submitted by\s+/u/\S+|/u/\S+\s*님이 제출|"
               r"\[(?:link|comments|링크|댓글)\]", " ", t)
    t = re.sub(r"https?://\S+", " ", t)              # 날것의 링크 주소는 읽을 게 없어서 뺌
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= EXCERPT_LEN else t[:EXCERPT_LEN].rstrip() + "…"


def fetch_dc_excerpt(url: str) -> str | None:
    if not site_ok("디시 미리보기"):
        return None
    raw = http_get(url, {"Referer": "https://gall.dcinside.com/"}, retries=0, timeout=12)
    site_result("디시 미리보기", raw is not None)
    if not raw:
        return None
    h = raw.decode("utf-8", "replace")
    m = (re.search(r'<div class="write_div"[^>]*>(.*?)<div class="(?:con_banner|recommend_kapcode|btn_recommend_box|recom_bottom_box|positionr)', h, re.S)
         or re.search(r'<div class="write_div"[^>]*>(.{0,20000})', h, re.S))
    return make_excerpt(m.group(1)) if m else ""


# ════════════════════════════════════════════
#  수집기 — 검색어 (구글·네이버·유튜브 자동완성, 트렌드, 나무위키)
# ════════════════════════════════════════════
def fetch_google_suggest(seed: str, hl: str, youtube: bool = False) -> list[str]:
    params = {"client": "firefox", "hl": hl, "ie": "utf-8", "oe": "utf-8", "q": seed}
    if youtube:
        params["ds"] = "yt"
    raw = http_get("https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(params), retries=1)
    try:
        return [s for s in json.loads(raw.decode("utf-8", "replace"))[1]] if raw else []
    except Exception:
        return []


def fetch_naver_suggest(seed: str) -> list[str]:
    params = {"q": seed, "con": "1", "frm": "nv", "ans": "2", "r_format": "json", "r_enc": "UTF-8",
              "r_unicode": "0", "t_koreng": "1", "run": "2", "rev": "4", "q_enc": "UTF-8", "st": "100"}
    raw = http_get("https://ac.search.naver.com/nx/ac?" + urllib.parse.urlencode(params), retries=1)
    try:
        return [x[0] for x in json.loads(raw)["items"][0]] if raw else []
    except Exception:
        return []


def fetch_namu_ranking() -> list[str]:
    """나무위키 실시간 인기 검색어 (막히면 빈 목록)."""
    raw = http_get("https://search.namu.wiki/api/ranking", {"Referer": "https://namu.wiki/"}, retries=1)
    try:
        data = json.loads(raw) if raw else []
        return [d if isinstance(d, str) else d.get("keyword", "") for d in data]
    except Exception:
        return []


def fetch_trends(geo: str) -> list[dict]:
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
    pat = alias_pattern(game["aliases"])
    items = []
    for t in trends:
        if pat.search(" ".join([t["title"], *t["news"]])):
            url = t["url"] or "https://trends.google.com/trending?geo=" + t["geo"]
            items.append(Item(game["id"], f"'{t['title']}' 급상승 검색어 ({t['geo']})", url,
                              "Google 트렌드", "trend", t["date"], traffic=t["traffic"], tags={"trend"}))
    return items


def kw_norm(k: str) -> str:
    return re.sub(r"\s+", "", k.lower())


def traffic_num(t: str) -> int:
    m = re.match(r"([\d,.]+)\s*([KkMm만천]?)", t or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "만": 1e4, "천": 1e3}.get(m.group(2), 1))


SOURCE_WEIGHTS = {"naver": 1.2, "google": 1.0, "youtube": 1.0, "google_en": 0.6, "namu": 2.5, "trends": 3.0}
SOURCE_LABELS = {"naver": "네이버", "google": "구글", "youtube": "유튜브", "google_en": "구글(영어)",
                 "namu": "나무위키", "trends": "구글 트렌드"}


def score_keywords(lists: dict[str, list[str]], game: dict, extra: dict[str, dict] | None = None) -> list[dict]:
    """
    여러 출처의 '인기순 목록'을 합쳐 검색어별 인기 점수를 매긴다.
      - 각 출처 목록에서 위에 있을수록 점수 ↑ (1위 = 1.0, 꼴찌 ≈ 0.1)
      - 여러 출처에 동시에 뜨면 보너스
      - 나무위키 인기 검색어 / 구글 트렌드 급상승은 가중치 큼
    그리고 게임 안에서 최고점 대비 비율로 색 단계를 정한다.
      hot(빨강) ≥ 70% · warm(주황) ≥ 50% · mild(노랑) ≥ 35% · cold(회색)
    """
    names = {kw_norm(a) for a in game["aliases"]} | {kw_norm(game["name"])}
    acc: dict[str, dict] = {}
    for src, words in lists.items():
        n = len(words)
        for i, w in enumerate(words):
            w = w.strip()
            k = kw_norm(w)
            if not k or k in names or len(k) < 2:
                continue
            e = acc.setdefault(k, {"k": w, "score": 0.0, "sources": {}})
            if src in e["sources"]:
                continue
            pos = 1.0 - 0.9 * (i / max(n - 1, 1))
            e["sources"][src] = i + 1
            e["score"] += SOURCE_WEIGHTS.get(src, 1.0) * pos
            if re.search(r"[가-힣]", w) and not re.search(r"[가-힣]", e["k"]):
                e["k"] = w        # 한국어 표기 우선
    for k, x in (extra or {}).items():   # 트렌드 등 외부 가산점
        e = acc.setdefault(k, {"k": x["k"], "score": 0.0, "sources": {}})
        e["score"] += x["score"]
        e["sources"].update(x["sources"])
    for e in acc.values():
        e["score"] += 0.6 * (len(e["sources"]) - 1)      # 여러 곳에 뜰수록 ↑
    ranked = sorted(acc.values(), key=lambda e: -e["score"])
    top = ranked[0]["score"] if ranked else 1
    for e in ranked:
        r = e["score"] / top
        e["tier"] = "hot" if r >= 0.70 else "warm" if r >= 0.50 else "mild" if r >= 0.35 else "cold"
        e["score"] = round(e["score"], 2)
        e["sources"] = sorted(e["sources"], key=lambda s: -SOURCE_WEIGHTS.get(s, 1))
    return ranked


def collect_keywords(cfg: dict, trends: list[dict], namu: list[str]) -> dict[str, list[dict]]:
    out = {}
    for g in cfg["games"]:
        if past_deadline():
            log.warning("시간 제한 — 남은 게임의 검색어는 다음에 갱신")
            break
        lists: dict[str, list[str]] = {"google": [], "naver": [], "youtube": [], "google_en": []}
        for seed in g.get("suggest_ko", []):
            lists["google"] += fetch_google_suggest(seed, "ko")
            lists["naver"] += fetch_naver_suggest(seed)
            lists["youtube"] += fetch_google_suggest(seed, "ko", youtube=True)
            time.sleep(SUGGEST_DELAY)
        for seed in g.get("suggest_en", []):
            lists["google_en"] += fetch_google_suggest(seed, "en")
            time.sleep(SUGGEST_DELAY)
        pat = alias_pattern(g["aliases"])
        lists["namu"] = [w for w in namu if pat.search(w)]
        extra = {}
        for t in trends:
            if pat.search(" ".join([t["title"], *t["news"]])):
                k = kw_norm(t["title"])
                extra[k] = {"k": t["title"], "sources": {"trends": 1},
                            "score": SOURCE_WEIGHTS["trends"] + math.log10(1 + traffic_num(t["traffic"])) / 2}
        lists = {s: list(dict.fromkeys(w.strip() for w in ws if w.strip())) for s, ws in lists.items()}
        out[g["id"]] = score_keywords(lists, g, extra)[:30]
        log.info("%s 검색어 %d개 (%s)", g["name"], len(out[g["id"]]),
                 ", ".join(f"{s} {len(v)}" for s, v in lists.items() if v))
    return out


# ════════════════════════════════════════════
#  번역 (구글 번역 무료 엔드포인트)
# ════════════════════════════════════════════
def needs_translation(text: str) -> bool:
    if re.search(r"[가-힣]", text):
        return False
    return bool(re.search(r"[A-Za-z぀-ヿ一-鿿]", text))


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    for src in sorted(glossary, key=len, reverse=True):
        pat = rf"\b{re.escape(src)}\b" if src.isascii() else re.escape(src)
        text = re.sub(pat, glossary[src], text, flags=re.IGNORECASE)
    return text


def source_lang(text: str) -> str:
    # 용어집으로 한국어 단어가 섞이면 '자동 감지'가 한국어로 착각해 번역을 안 해서, 원문 언어를 직접 정함
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text) and not re.search(r"[A-Za-z]{3,}", text):
        return "zh-CN"
    return "en"


def _parse_gtx(raw: bytes) -> str:
    return "".join(seg[0] for seg in json.loads(raw)[0] if seg and seg[0]).strip()


def _parse_clients5(raw: bytes) -> str:
    data = json.loads(raw)
    first = data[0] if data else ""
    return (first if isinstance(first, str) else first[0]).strip()


# GitHub 서버는 여러 사람이 같이 써서 한 주소가 자주 막힘(429) → 막히면 다음 주소로
TRANSLATORS = [
    ("구글 번역", "https://translate.googleapis.com/translate_a/single", {"client": "gtx", "dt": "t"}, _parse_gtx),
    ("구글 번역(2)", "https://translate.google.com/translate_a/single", {"client": "gtx", "dt": "t"}, _parse_gtx),
    ("구글 번역(3)", "https://clients5.google.com/translate_a/t", {"client": "dict-chrome-ex"}, _parse_clients5),
]


def translate_ok() -> bool:
    return any(site_ok(name) for name, *_ in TRANSLATORS)


def translate_ko(text: str, glossary: dict[str, str]) -> str | None:
    q = apply_glossary(text, glossary)
    for name, base, params, parse in TRANSLATORS:
        if not site_ok(name):
            continue
        url = base + "?" + urllib.parse.urlencode({**params, "sl": source_lang(text), "tl": "ko", "q": q})
        raw = http_get(url, retries=0, timeout=10)
        site_result(name, raw is not None)
        if not raw:
            continue
        try:
            out = parse(raw)
        except Exception:
            continue
        if out:
            return out
    return None


def translate_items(items: list[Item], cache: dict[str, str], glossary: dict[str, str]) -> int:
    done = 0
    for it in items:
        if not translate_ok():
            break
        if not needs_translation(it.title):
            continue
        if it.title in cache:
            it.title_ko = cache[it.title]
            continue
        ko = translate_ko(it.title, glossary)
        if ko:
            it.title_ko = cache[it.title] = ko
            done += 1
        time.sleep(TRANSLATE_DELAY)
    return done


# ════════════════════════════════════════════
#  분류 / 점수 / 중복 제거
# ════════════════════════════════════════════
def compile_keywords(kw: dict) -> dict[str, re.Pattern]:
    out = {}
    for cat, words in kw.items():
        parts = []
        for w in words:
            esc = re.escape(w.lower())
            if w.isascii():
                esc = (r"\b" if w[0].isalnum() else "") + esc + (r"\b" if w[-1].isalnum() else "")
            parts.append(esc)
        out[cat] = re.compile("|".join(parts) or r"(?!x)x", re.IGNORECASE)
    return out


LEAK_FLAIRS = {"reliable": "높음", "datamined": "높음", "questionable": "보통",
               "rumor": "낮음", "unreliable": "낮음", "fake": "낮음"}


def classify(item: Item, pats: dict[str, re.Pattern]) -> None:
    text = item.title + " " + item.flair
    flair = item.flair.lower()
    if pats["leak"].search(text) or "leak" in item.source.lower():
        item.tags.add("leak")
    if pats["official"].search(text) or "official" in flair or "news" in flair or "공지" in flair:
        item.tags.add("official")
    if pats["hot"].search(text):
        item.tags.add("hot")
    if "official" in flair:
        item.tags.discard("leak")


def reliability(item: Item) -> str:
    for k, v in LEAK_FLAIRS.items():
        if k in item.flair.lower():
            return v
    return "확인 안 됨"


def score(item: Item, now: datetime) -> float:
    s = 1.0
    if item.kind == "reddit" and item.rank >= 0 and not item.upvotes:
        s += max(0.0, 5.0 - 0.25 * item.rank)
    elif item.kind == "reddit":
        s += 2 * math.log10(1 + item.upvotes) + math.log10(1 + item.comments)
    elif item.kind == "community":
        # 디시 추천·아카 추천은 Reddit 보다 숫자 단위가 작아서 가중치 ↑
        s += 2.6 * math.log10(1 + item.upvotes) + math.log10(1 + item.comments) + 0.5 * math.log10(1 + item.views)
    elif item.kind == "trend":
        s += 8
    else:
        s += 1.2 * (item.coverage - 1)
    s += 1.5 * ("leak" in item.tags) + 1.0 * ("official" in item.tags) + 1.2 * ("hot" in item.tags)
    if item.published and now - item.published < timedelta(hours=6):
        s += 1.0
    if item.kind == "news" and re.search(r"[가-힣]", item.title):
        s += KOREAN_BONUS
    return round(s, 2)


def merge_duplicates(items: list[Item]) -> list[Item]:
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
#  저장소 (상태 + 누적 소식)
# ════════════════════════════════════════════
def load_json(p: Path, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def save_json(p: Path, obj, compact: bool = False) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    if compact:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), "utf-8")
    else:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def item_to_dict(it: Item, first_seen: str) -> dict:
    return {
        "id": it.key, "game": it.game, "title": it.title, "title_ko": it.title_ko or None,
        "url": it.url, "source": it.source, "kind": it.kind,
        "published": it.published.isoformat() if it.published else None, "first_seen": first_seen,
        "upvotes": it.upvotes, "comments": it.comments, "views": it.views, "flair": it.flair,
        "traffic": it.traffic, "coverage": it.coverage, "score": it.score, "tags": sorted(it.tags),
        "section": primary_section(it),
        "reliability": reliability(it) if "leak" in it.tags and "trend" not in it.tags else None,
        # excerpt 키가 없으면 '아직 미리보기 안 가져옴' (디시는 나중 회차에 채움)
        **({"excerpt": it.excerpt} if it.excerpt else {}),
    }


def dict_to_item(d: dict) -> Item:
    it = Item(d["game"], d["title"], d["url"], d["source"], d.get("kind", "news"),
              parse_date(d.get("published")), upvotes=d.get("upvotes", 0), comments=d.get("comments", 0),
              views=d.get("views", 0), flair=d.get("flair", ""), traffic=d.get("traffic", ""),
              title_ko=d.get("title_ko") or "", tags=set(d.get("tags", [])), score=d.get("score", 0),
              excerpt=d.get("excerpt") or "")
    it.outlets = {f"#{i}" for i in range(max(1, d.get("coverage", 1)))}   # 매체 수만 필요
    return it


# ════════════════════════════════════════════
#  한 회차 수집
# ════════════════════════════════════════════
def collect(cfg: dict, data_dir: Path, now: datetime, force_keywords: bool = False) -> dict:
    st = cfg["settings"]
    state_dir = data_dir / "state"
    pats = compile_keywords(cfg["keywords"])
    flair_ex = compile_keywords({"x": cfg["keywords"].get("exclude_flair", [])})["x"]
    glossary = cfg.get("glossary", {})
    tr_cache: dict[str, str] = load_json(state_dir / "translations.json", {})
    # 예전 버그로 '용어집만 바뀌고 번역 안 된' 제목은 버리고 다시 번역
    tr_cache = {k: v for k, v in tr_cache.items() if v != apply_glossary(k, cfg.get("glossary", {}))}
    meta = load_json(state_dir / "meta.json", {})
    store: dict[str, dict] = {d["id"]: d for d in load_json(data_dir / "site" / "data" / "items.json",
                                                             {"items": []}).get("items", [])}
    migrated: dict[str, dict] = {}
    for d in store.values():
        if d.get("title_ko") and d["title_ko"] == apply_glossary(d["title"], cfg.get("glossary", {})):
            d["title_ko"] = None
        if d.get("excerpt"):
            d["excerpt"] = make_excerpt(d["excerpt"]) or None
        d["id"] = item_key(d["game"], d.get("kind", "news"), d["title"], d["url"])
        if d["id"] not in migrated or d["first_seen"] < migrated[d["id"]]["first_seen"]:
            migrated[d["id"]] = d
    store = migrated
    news_since = now - timedelta(hours=st.get("lookback_hours", 14))
    comm_since = now - timedelta(hours=st.get("community_hours", 24))
    now_iso = now.isoformat(timespec="seconds")

    trends = []
    for geo in st.get("trend_geos", ["KR"]):
        trends += fetch_trends(geo) if not past_deadline() else []
    log.info("구글 트렌드 %d개", len(trends))

    new_counts: dict[str, int] = {}
    for g in cfg["games"]:
        raw: list[Item] = []
        for q in g.get("news_ko", []):
            raw += fetch_google_news(g, q, "ko", news_since)
            raw += fetch_naver_news(g, q, news_since)
        for q in g.get("news_en", []):
            raw += fetch_google_news(g, q, "en", news_since)
        if g.get("dcinside"):
            raw += fetch_dcinside(g, g["dcinside"], comm_since)
        if g.get("arca"):
            raw += fetch_arca(g, g["arca"], comm_since)
        for sub in g.get("subreddits", []):
            raw += fetch_reddit(g, sub, comm_since)
        for ch in g.get("youtube_channels", []):
            raw += fetch_youtube(g, ch, news_since)
        raw += match_trends(g, trends)

        apat = alias_pattern(g["aliases"])
        raw = [it for it in raw
               if it.url.startswith(("https://", "http://"))                     # 이상한 주소 제외
               and not (it.kind == "news" and not apat.search(it.title))          # 게임 이름 없는 기사 제외
               and not (it.kind != "trend" and pats["exclude"].search(it.title))  # 팬아트 등 제외
               and not (it.flair and flair_ex.search(it.flair))]                  # 말머리로 제외
        items = merge_duplicates(raw)
        for it in items:
            classify(it, pats)
            it.score = score(it, now)

        fresh = []
        for it in items:
            old = store.get(it.key)
            if old:   # 이미 아는 글 → 추천수·점수만 갱신
                old.update(upvotes=max(old.get("upvotes", 0), it.upvotes),
                           comments=max(old.get("comments", 0), it.comments),
                           views=max(old.get("views", 0), it.views),
                           coverage=max(old.get("coverage", 1), it.coverage))
                old["score"] = max(old.get("score", 0), it.score)
            else:
                fresh.append(it)
        # 출처 종류별로 따로 상한 (커뮤니티 글이 뉴스를 밀어내지 않게)
        fresh.sort(key=lambda x: -x.score)
        caps, used, kept = MAX_NEW_PER_KIND, {}, []
        for it in fresh:
            used[it.kind] = used.get(it.kind, 0) + 1
            if used[it.kind] <= caps.get(it.kind, 30):
                kept.append(it)
        fresh = kept
        if st.get("translate", True):
            n = translate_items(fresh, tr_cache, glossary)
            if n:
                log.info("%s: 제목 %d개 번역", g["name"], n)
        for it in fresh:
            store[it.key] = item_to_dict(it, now_iso)
        new_counts[g["id"]] = len(fresh)
        log.info("%s: 수집 %d → 새 소식 %d", g["name"], len(items), len(fresh))

    # 디시 글 본문 첫머리 가져오기 (점수 높은 것부터, 회차당 PREVIEW_MAX 개)
    todo = sorted((d for d in store.values() if d["source"] == "디시 개념글" and "excerpt" not in d),
                  key=lambda d: -d.get("score", 0))[:PREVIEW_MAX]
    got = 0
    for d in todo:
        ex = fetch_dc_excerpt(d["url"])
        if ex is None:
            if not site_ok("디시 미리보기"):
                break
            continue
        d["excerpt"] = ex or None
        got += 1
        time.sleep(COMMUNITY_DELAY / 2)
    if got:
        log.info("디시 글 미리보기 %d개", got)

    # 지난 회차에 번역 못 한 제목 채우기 (번역이 막혔던 경우)
    if st.get("translate", True) and translate_ok():
        todo = sorted((d for d in store.values() if not d.get("title_ko") and needs_translation(d["title"])),
                      key=lambda d: -d.get("score", 0))[:TRANSLATE_BACKFILL]
        filled = 0
        for d in todo:
            ko = tr_cache.get(d["title"]) or translate_ko(d["title"], glossary)
            if not ko:
                if not translate_ok():
                    break
                continue
            d["title_ko"] = tr_cache[d["title"]] = ko
            filled += 1
            time.sleep(TRANSLATE_DELAY)
        if filled:
            log.info("이전 소식 제목 %d개 추가 번역", filled)

    # 오래된 소식 정리
    cutoff = (now - timedelta(days=st.get("store_days", 14))).isoformat()
    items_out = sorted((d for d in store.values() if d["first_seen"] >= cutoff),
                       key=lambda d: d["first_seen"], reverse=True)[:st.get("store_max_items", 3000)]

    # 검색어는 keyword_refresh_minutes 마다만 새로 (요청 수 줄이기)
    keywords = load_json(data_dir / "site" / "data" / "keywords.json", {})
    last_kw = meta.get("keywords_at")
    due = (force_keywords or not last_kw or not keywords.get("games")
           or now - datetime.fromisoformat(last_kw) >= timedelta(minutes=st.get("keyword_refresh_minutes", 30)))
    if due:
        namu = fetch_namu_ranking()
        log.info("나무위키 인기 검색어 %d개", len(namu))
        prev = {gid: {kw_norm(e["k"]) for e in lst} for gid, lst in keywords.get("games", {}).items()}
        games_kw = {**keywords.get("games", {}), **collect_keywords(cfg, trends, namu)}
        for gid, lst in games_kw.items():
            for e in lst:
                e["new"] = bool(prev.get(gid)) and kw_norm(e["k"]) not in prev[gid]
        keywords = {"updated": now_iso, "namu_ok": bool(namu), "games": games_kw}
        meta["keywords_at"] = now_iso

    return {"now": now, "items": items_out, "new_counts": new_counts, "keywords": keywords,
            "translations": dict(list(tr_cache.items())[-3000:]), "meta": meta}


# ════════════════════════════════════════════
#  알림용 묶음 (마지막 알림 이후 새 소식)
# ════════════════════════════════════════════
SECTION_TITLES = {"official": "📢 공식", "leak": "🕵️ 떡밥·유출", "hot": "🔥 화제"}
TIER_EMOJI = {"hot": "🔴", "warm": "🟠", "mild": "🟡", "cold": "⚪"}


def build_digest(cfg: dict, result: dict, since_iso: str) -> dict:
    per = cfg["settings"].get("max_per_section", 3)
    games = []
    total = 0
    for g in cfg["games"]:
        its = [dict_to_item(d) for d in result["items"] if d["game"] == g["id"] and d["first_seen"] > since_iso]
        its.sort(key=lambda x: -x.score)
        sections = {"official": [], "leak": [], "hot": []}
        for it in its:
            sec = sections[primary_section(it)]
            if len(sec) < per:
                sec.append(it)
        kws = result["keywords"].get("games", {}).get(g["id"], [])
        total += len(its)
        games.append({"cfg": g, "sections": sections, "fresh": len(its), "keywords": kws[:8],
                      "new_keywords": [e for e in kws if e.get("new")][:6]})
    return {"now": result["now"], "games": games, "new_count": total}


def display_title(it: Item) -> str:
    return it.title_ko or it.title


def fmt_item(it: Item, now: datetime, style: str) -> str:
    t = display_title(it)
    title = t if len(t) <= 90 else t[:88] + "…"
    meta = [it.source]
    if it.kind in ("reddit", "community") and it.upvotes:
        meta.append(f"👍{short_num(it.upvotes)} 💬{short_num(it.comments)}")
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


def fmt_keywords(kws: list[dict]) -> str:
    return ", ".join(f"{TIER_EMOJI.get(e.get('tier'), '')}{e['k']}" for e in kws)


def game_block(gd: dict, now: datetime, style: str) -> str:
    lines = []
    b = (lambda s: f"<b>{html.escape(s)}</b>") if style == "html" else (lambda s: f"**{s}**")
    esc = html.escape if style == "html" else (lambda s: s)
    if gd["fresh"] == 0:
        lines.append("새 소식 없음")
    for sec, title in SECTION_TITLES.items():
        its = gd["sections"][sec]
        if its:
            lines.append(b(title))
            lines += [fmt_item(it, now, style) for it in its]
    if gd["keywords"]:
        lines.append(f"{b('🔍 많이 찾는 검색어')} {esc(fmt_keywords(gd['keywords']))}")
    if gd["new_keywords"]:
        lines.append(f"{b('🆕 새로 뜬 검색어')} {esc(fmt_keywords(gd['new_keywords']))}")
    return "\n".join(lines)


def top3(report: dict) -> list[tuple[dict, Item]]:
    allits = [(gd["cfg"], it) for gd in report["games"] for sec in gd["sections"].values() for it in sec]
    return sorted(allits, key=lambda x: -x[1].score)[:3]


def header(report: dict) -> str:
    now = report["now"]
    return (f"🎮 서브컬쳐 떡밥 레이더 — {now:%Y-%m-%d} ({WEEKDAYS[now.weekday()]}) "
            f"{slot_name(now)} · {now:%H:%M} KST · 새 소식 {report['new_count']}개")


def render_markdown(report: dict, site_url: str = "") -> str:
    now = report["now"]
    out = [f"# {header(report)}", ""]
    if site_url:
        out += [f"🌐 전체 보기: {site_url}", ""]
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


def send_discord(report: dict, webhook: str, site_url: str = "") -> None:
    now = report["now"]
    t = top3(report)
    top_txt = "\n".join(f"{i}. {g['emoji']} " + fmt_item(it, now, "md")[2:]
                        for i, (g, it) in enumerate(t, 1)) or "이번엔 새 소식이 없어요."
    if site_url:
        top_txt += f"\n\n🌐 [사이트에서 전체 보기]({site_url})"
    http_post_json(webhook, {"username": "서브컬쳐 레이더",
                             "embeds": [{"title": header(report), "description": top_txt[:4000], "color": 0xFF5C8A}]})
    for gd in report["games"]:
        g = gd["cfg"]
        if gd["fresh"] == 0 and not gd["new_keywords"]:
            continue
        http_post_json(webhook, {"username": "서브컬쳐 레이더",
                                 "embeds": [{"title": f"{g['emoji']} {g['name']}",
                                             "description": game_block(gd, now, "md")[:4000],
                                             "color": int(g.get("color", "5865F2"), 16)}]})
        time.sleep(0.7)


def send_telegram(report: dict, token: str, chat_id: str, site_url: str = "") -> None:
    now = report["now"]
    parts = [f"<b>{html.escape(header(report))}</b>"]
    if site_url:
        parts.append(f'🌐 <a href="{html.escape(site_url)}">사이트에서 전체 보기</a>')
    t = top3(report)
    if t:
        parts.append("<b>🏆 TOP 3</b>\n" + "\n".join(
            f"{i}. {g['emoji']} " + fmt_item(it, now, "html")[2:] for i, (g, it) in enumerate(t, 1)))
    for gd in report["games"]:
        g = gd["cfg"]
        parts.append(f"<b>{g['emoji']} {html.escape(g['name'])}</b>\n" + game_block(gd, now, "html"))
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


def notify_slot_due(cfg: dict, now: datetime, meta: dict) -> str | None:
    """지금이 알림 시각(+여유 시간) 안이고 아직 안 보냈으면 그 슬롯 이름을 돌려준다."""
    st = cfg["settings"]
    window = timedelta(minutes=st.get("notify_window_minutes", 60))
    for hm in st.get("notify_times", ["07:50", "11:50", "18:50"]):
        h, m = map(int, hm.split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        slot = f"{now:%Y-%m-%d} {hm}"
        if t <= now < t + window and meta.get("last_notify_slot") != slot:
            return slot
    return None


# ════════════════════════════════════════════
#  사이트 데이터 쓰기 + 실행
# ════════════════════════════════════════════
def write_site_data(cfg: dict, data_dir: Path, result: dict) -> None:
    d = data_dir / "site" / "data"
    now_iso = result["now"].isoformat(timespec="seconds")
    save_json(d / "items.json", {"updated": now_iso, "items": result["items"]}, compact=True)
    save_json(d / "keywords.json", result["keywords"], compact=True)
    idx = load_json(d / "index.json", {})
    runs = [{"t": now_iso, "n": sum(result["new_counts"].values()), "c": result["new_counts"]}] + idx.get("runs", [])
    week_ago = (result["now"] - timedelta(days=7)).isoformat()
    st = cfg["settings"]
    save_json(d / "index.json", {
        "updated": now_iso,
        "interval_minutes": st.get("interval_minutes", 10),
        "notify_times": st.get("notify_times", []),
        "games": [{"id": g["id"], "name": g["name"], "emoji": g["emoji"], "color": g.get("color", "888888")}
                  for g in cfg["games"]],
        "runs": [r for r in runs if r["t"] >= week_ago],
    }, compact=True)


def run_once(cfg: dict, data_dir: Path, dry_run: bool = False, force_notify: bool = False) -> str:
    global DEADLINE
    now = datetime.now(KST)
    _blocked.clear()
    DEADLINE = time.time() + cfg["settings"].get("max_run_seconds", 300)
    result = collect(cfg, data_dir, now, force_keywords=force_notify)
    meta = result["meta"]
    site_url = cfg["settings"].get("site_url", "")

    slot = "manual" if force_notify else notify_slot_due(cfg, now, meta)
    since = meta.get("last_notify_at") or (now - timedelta(hours=12)).isoformat()
    digest = build_digest(cfg, result, since if slot else (now - timedelta(minutes=1)).isoformat())
    md = render_markdown(digest, site_url)

    if dry_run:
        return render_markdown(build_digest(cfg, result, (now - timedelta(hours=24)).isoformat()), site_url)

    write_site_data(cfg, data_dir, result)
    save_json(data_dir / "state" / "translations.json", result["translations"], compact=True)
    if slot:
        (data_dir / "reports").mkdir(parents=True, exist_ok=True)
        (data_dir / "reports" / "latest.md").write_text(md, "utf-8")
        if hook := os.getenv("DISCORD_WEBHOOK_URL"):
            send_discord(digest, hook, site_url)
            log.info("디스코드 알림 전송")
        tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if tg_token and tg_chat:
            send_telegram(digest, tg_token, tg_chat, site_url)
            log.info("텔레그램 알림 전송")
        if slot != "manual":
            meta["last_notify_slot"] = slot
        meta["last_notify_at"] = now.isoformat(timespec="seconds")
    save_json(data_dir / "state" / "meta.json", meta)
    return md


def loop(cfg: dict, data_dir: Path) -> None:
    interval = cfg["settings"].get("interval_minutes", 10)
    log.info("상시 실행 모드 — %d분마다 수집", interval)
    while True:
        started = time.time()
        try:
            run_once(cfg, data_dir)
        except Exception:
            log.exception("수집 중 오류 — 다음 회차에 다시 시도합니다")
        time.sleep(max(30, interval * 60 - (time.time() - started)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="서브컬쳐 게임 떡밥 레이더")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--data-dir", default=None, help="상태·사이트 데이터 폴더 (기본: config 와 같은 폴더)")
    ap.add_argument("--dry-run", action="store_true", help="저장·전송 없이 최근 24시간 결과만 출력")
    ap.add_argument("--notify-now", action="store_true", help="알림 시각이 아니어도 지금 알림 보내기")
    ap.add_argument("--loop", action="store_true", help="interval_minutes 마다 계속 실행")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = tomllib.loads(Path(args.config).read_text("utf-8"))
    data_dir = Path(args.data_dir) if args.data_dir else Path(args.config).resolve().parent

    if args.loop:
        loop(cfg, data_dir)
        return 0
    print(run_once(cfg, data_dir, dry_run=args.dry_run, force_notify=args.notify_now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
