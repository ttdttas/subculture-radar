"""인터넷 없이 가짜 응답으로 전체 흐름을 검사하는 테스트.
실행:  python -m unittest discover -s tests -v
"""
import json
import shutil
import sys
import tempfile
import time
import tomllib
import unittest
import urllib.parse as up
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import radar  # noqa: E402

NOW = datetime.now(timezone.utc)
KNOW = datetime.now(radar.KST)


def rss(items):
    body = "".join(
        f"<item><title>{t} - {s}</title><link>{l}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=h))}</pubDate><source url='x'>{s}</source></item>"
        for t, l, s, h in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


TRENDS = ("<?xml version='1.0'?><rss xmlns:ht='https://trends.google.com/trending/rss'><channel>"
          "<item><title>원신 코드</title><ht:approx_traffic>20000+</ht:approx_traffic>"
          f"<pubDate>{format_datetime(NOW)}</pubDate><ht:news_item><ht:news_item_title>x</ht:news_item_title>"
          "<ht:news_item_url>https://ex.com/t</ht:news_item_url></ht:news_item></item></channel></rss>").encode()


def dc_row(no, subj, title, rec, reply, views, hours_ago, typ="icon_txt"):
    d = (KNOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return (f'<tr class="ub-content us-post" data-no="{no}" data-type="{typ}" >'
            f'<td class="gall_num">{no}</td><td class="gall_subject"><b>{subj}</b></td>'
            f'<td class="gall_tit ub-word"><a href="/mgallery/board/view/?id=x&no={no}"><em></em>{title}</a>'
            f'<a class="reply_numbox" href="#"><span class="reply_num">[{reply}]</span></a></td>'
            f'<td class="gall_writer">w</td><td class="gall_date" title="{d}">11.01</td>'
            f'<td class="gall_count">{views}</td><td class="gall_recommend">{rec}</td></tr>')


DC_HTML = ("<table>" + dc_row(1, "공지", "공지글", 9, 9, 9, 1, "icon_notice")
           + dc_row(2, "일반", "원신 7.3 신캐 스킬 정리", 55, 40, 3000, 2)
           + dc_row(3, "🎨창작", "그림 그려옴", 80, 10, 2000, 3)
           + dc_row(4, "일반", "🔞 후방주의", 90, 10, 2000, 3)
           + dc_row(5, "일반", "오래된 글", 90, 10, 2000, 50) + "</table>").encode()


def arca_row(no, badge, title, rate, cmt, views, hours_ago, notice=False):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cls = " notice notice-board" if notice else ""
    return (f'<a class="vrow column{cls}" href="/b/genshin/{no}?mode=best&amp;p=1" >'
            f'<div class="vrow-inner"><div class="vrow-top"><span class="vcol col-title">'
            f'<span class="badges"><span class="badge text-bg-success">{badge}</span></span>'
            f'<span class="title"><span class="bi-star-fill"></span> {title} </span>'
            f'<span class="info"><span class="comment-count">[{cmt}]</span></span></span></div>'
            f'<div class="vrow-bottom"><span class="vcol col-time"><time datetime="{t}">x</time></span>'
            f'<span class="vcol col-view">{views}</span><span class="vcol col-rate">{rate}</span></div></div>'
            f'</a>\n')


ARCA_HTML = ("<div>" + arca_row(10, "공지", "채널 규정", 0, 0, 1, 1, notice=True)
             + arca_row(11, "찌라시", "스포) 다음 버전 스토리", 24, 32, 2137, 2)
             + arca_row(12, "정보", "원신 7.2 특별 방송 요약", 60, 20, 5000, 1) + "</div>").encode()


def fake_get(url, headers=None, data=None, timeout=20, retries=2):
    if "trends.google.com" in url:
        return TRENDS if "geo=KR" in url else None
    if "news.google.com" in url:
        q = up.parse_qs(up.urlparse(url).query)["q"][0]
        if q.startswith("원신 when"):
            return rss([
                ("원신 6.2 버전 특별 방송 일정 공개", "https://ex.com/a", "인벤", 2),
                ("원신 6.2 버전 특별방송 일정 공개됐다", "https://ex.com/b", "루리웹", 3),
                ("원신 신규 캐릭터 유출 논란", "https://ex.com/c", "게임메카", 5),
                ("아주 오래된 원신 기사", "https://ex.com/old", "옛날신문", 40),
            ])
        return rss([])
    if "gall.dcinside.com" in url and "board/view" in url:
        return ('<div class="write_div" style="x"><script>var a={{each}};</script><p>7.3 신캐 스킬은 '
                '이렇게 바뀜<br>궁극기 계수 상향</p></div><div class="btn_recommend_box">').encode()
    if "gall.dcinside.com" in url:
        return DC_HTML if "onshinproject" in url else b"<table></table>"
    if "arca.live" in url:
        return ARCA_HTML if "/b/genshin" in url else b"<div></div>"
    if "reddit.com" in url and "rss" in url:
        if "Genshin_Impact_Leaks" in url:
            return (f"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry>"
                    f"<title>[6.3 Beta] New 5-star kit via Dim</title><link href='https://www.reddit.com/r/x/1'/>"
                    f"<updated>{NOW.isoformat()}</updated></entry></feed>").encode()
        return b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'></feed>"
    if "translate.googleapis.com" in url:
        q = up.parse_qs(up.urlparse(url).query)["q"][0]
        return json.dumps([[["[KO] " + q, q, None, None]], None, "en"]).encode()
    if "suggestqueries" in url:
        p = up.parse_qs(up.urlparse(url).query)
        q = p["q"][0].strip()
        if "ds" in p:   # 유튜브
            return json.dumps([q, [f"{q} 스토리", f"{q} 픽업", f"{q} 티어"]]).encode()
        return json.dumps([q, [f"{q} 픽업", f"{q} 캐릭터", f"{q} 스토리", f"{q} 갤"]]).encode()
    if "ac.search.naver.com" in url:
        q = up.parse_qs(up.urlparse(url).query)["q"][0].strip()
        return json.dumps({"items": [[[f"{q} 픽업", "0"], [f"{q} 갤", "0"], [f"{q} 카페", "0"]]]}).encode()
    if "namu.wiki" in url:
        return json.dumps(["원신 픽업", "다른 검색어"]).encode()
    return None


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))
        self._orig = radar.http_get
        radar.http_get = fake_get
        radar._reddit_token = None
        radar.DEADLINE = 0
        for k in ("REDDIT_DELAY", "COMMUNITY_DELAY", "TRANSLATE_DELAY", "SUGGEST_DELAY"):
            setattr(radar, k, 0)

    def tearDown(self):
        radar.http_get = self._orig
        shutil.rmtree(self.tmp)

    def items(self):
        return json.loads((self.tmp / "site" / "data" / "items.json").read_text("utf-8"))["items"]

    def test_full_pipeline(self):
        md = radar.run_once(self.cfg, self.tmp, force_notify=True)
        print("\n" + md)
        items = self.items()
        titles = [i["title"] for i in items]
        gen = [i for i in items if i["game"] == "genshin"]

        self.assertIn("원신 7.3 신캐 스킬 정리", titles)            # 디시 개념글
        self.assertNotIn("공지글", titles)                          # 공지 제외
        self.assertNotIn("그림 그려옴", titles)                     # 창작 말머리 제외
        self.assertNotIn("🔞 후방주의", titles)                     # 후방 제외
        self.assertNotIn("오래된 글", titles)                       # 기간 밖 제외
        self.assertIn("스포) 다음 버전 스토리", titles)             # 아카 베스트
        self.assertNotIn("채널 규정", titles)
        leak = next(i for i in gen if i["title"] == "스포) 다음 버전 스토리")
        self.assertEqual(leak["section"], "leak")                   # '찌라시' 탭 → 떡밥
        news = next(i for i in gen if i["title"].startswith("원신 6.2"))
        self.assertEqual(news["coverage"], 2)                       # 비슷한 기사 합침
        self.assertNotIn("아주 오래된 원신 기사", titles)
        rd = next(i for i in gen if i["source"] == "r/Genshin_Impact_Leaks")
        self.assertTrue(rd["title_ko"].startswith("[KO] "))         # 영어 → 번역
        self.assertIsNone(news["title_ko"])                         # 한국어는 그대로
        self.assertTrue(any(i["kind"] == "trend" for i in gen))     # 트렌드
        dc = next(i for i in gen if i["title"] == "원신 7.3 신캐 스킬 정리")
        self.assertEqual(dc["excerpt"], "7.3 신캐 스킬은 이렇게 바뀜 궁극기 계수 상향")   # 본문 미리보기 (스크립트 제거)

        kw = json.loads((self.tmp / "site" / "data" / "keywords.json").read_text("utf-8"))
        gk = kw["games"]["genshin"]
        top = gk[0]
        self.assertEqual(top["k"], "원신 픽업")                     # 구글·네이버·유튜브·나무 모두 → 1위
        self.assertEqual(top["tier"], "hot")
        self.assertIn("namu", top["sources"])
        self.assertTrue(any(e["tier"] in ("mild", "cold") for e in gk))
        self.assertIn("🔴원신 픽업", md)                            # 알림에 색 이모지

        idx = json.loads((self.tmp / "site" / "data" / "index.json").read_text("utf-8"))
        self.assertEqual(len(idx["runs"]), 1)
        self.assertGreater(idx["runs"][0]["n"], 0)

        # 두 번째 실행: 같은 글은 새 소식이 아니고, 추천수만 갱신
        n_before = len(items)
        radar.run_once(self.cfg, self.tmp)
        idx = json.loads((self.tmp / "site" / "data" / "index.json").read_text("utf-8"))
        self.assertEqual(idx["runs"][0]["n"], 0)
        self.assertEqual(len(self.items()), n_before)

    def test_notify_slot(self):
        meta = {}
        at = lambda h, m: datetime(2026, 11, 2, h, m, tzinfo=radar.KST)
        self.assertEqual(radar.notify_slot_due(self.cfg, at(7, 55), meta), "2026-11-02 07:50")
        self.assertIsNone(radar.notify_slot_due(self.cfg, at(9, 0), meta))       # 창 밖
        self.assertIsNone(radar.notify_slot_due(self.cfg, at(7, 40), meta))      # 아직 전
        meta["last_notify_slot"] = "2026-11-02 07:50"
        self.assertIsNone(radar.notify_slot_due(self.cfg, at(8, 5), meta))       # 이미 보냄

    def test_keyword_tiers(self):
        g = {"aliases": ["원신"], "name": "원신"}
        ranked = radar.score_keywords({
            "google": ["원신 a", "원신 b", "원신 c", "원신 d"],
            "naver": ["원신 a", "원신 c"],
            "youtube": ["원신 a"],
        }, g)
        self.assertEqual(ranked[0]["k"], "원신 a")
        self.assertEqual(ranked[0]["tier"], "hot")
        self.assertEqual(ranked[-1]["tier"], "cold")
        self.assertEqual([e for e in ranked if e["k"] == "원신"], [])          # 게임 이름 자체 제외

    def test_keyword_boundaries(self):
        pats = radar.compile_keywords(self.cfg["keywords"])
        self.assertIsNone(pats["official"].search("how to prevent this"))
        self.assertIsNotNone(pats["leak"].search("HSR leaks: new path"))
        self.assertIsNotNone(pats["exclude"].search("Hanako (art by @someone)"))
        self.assertIsNone(pats["exclude"].search("Ocean event schedule"))

    def test_alias_match(self):
        p = radar.alias_pattern(["원신", "Genshin"])
        self.assertIsNotNone(p.search("‘원신’의 신규 캐릭터"))
        self.assertIsNone(p.search("주민센터 복원신청 안내"))
        self.assertIsNone(p.search("GenshinX"))

    def test_glossary(self):
        g = self.cfg["glossary"]
        self.assertEqual(radar.apply_glossary("Genshin Impact 7.2 leaks", g), "원신 7.2 leaks")
        self.assertEqual(radar.apply_glossary("HSR Kafka", g), "스타레일 카프카")
        self.assertFalse(radar.needs_translation("원신 7.2 업데이트"))
        self.assertTrue(radar.needs_translation("ブルアカ新生徒"))
        self.assertEqual(radar.source_lang("Honkai: Star Rail 4.7 launches"), "en")
        self.assertEqual(radar.source_lang("ブルアカ新生徒"), "ja")
        self.assertEqual(radar.source_lang("原神 新角色"), "zh-CN")

    def test_block_skip(self):
        calls = []
        radar.http_get = lambda url, *a, **k: calls.append(url) or None
        radar._blocked.clear()
        g = self.cfg["games"][1]
        since = datetime.now(timezone.utc) - timedelta(hours=14)
        for gid in ["a", "b", "c", "d"]:
            radar.fetch_dcinside(g, gid, since)
        self.assertEqual(len(calls), 2)     # 2번 막히면 그 회차는 더 안 감

    def test_translate_backfill(self):
        # 번역이 막힌 회차 → 원문만 저장, 다음 회차에 채워짐
        orig = fake_get
        radar.http_get = lambda url, *a, **k: None if "translate.googleapis" in url else orig(url, *a, **k)
        radar.run_once(self.cfg, self.tmp)
        rd = next(i for i in self.items() if i["source"] == "r/Genshin_Impact_Leaks")
        self.assertIsNone(rd["title_ko"])
        radar.http_get = fake_get
        radar.run_once(self.cfg, self.tmp)
        rd = next(i for i in self.items() if i["source"] == "r/Genshin_Impact_Leaks")
        self.assertTrue(rd["title_ko"].startswith("[KO] "))

    def test_news_block_and_deadline(self):
        calls = []
        radar.http_get = lambda url, *a, **k: calls.append(url) or None
        radar._blocked.clear()
        g = self.cfg["games"][1]
        since = datetime.now(timezone.utc) - timedelta(hours=14)
        for q in ["a", "b", "c", "d"]:
            radar.fetch_google_news(g, q, "ko", since)
        self.assertEqual(len(calls), 2)          # 구글 뉴스 2번 막히면 건너뜀
        radar._blocked.clear()
        radar.DEADLINE = time.time() - 1         # 시간 초과면 아예 요청 안 함
        radar.fetch_google_news(g, "e", "ko", since)
        radar.DEADLINE = 0
        self.assertEqual(len(calls), 2)

    def test_item_key(self):
        # 짧은 제목이 같아도 다른 글(주소가 다름)이면 다른 키, 뉴스는 매체 달라도 같은 제목이면 같은 키
        a = radar.Item("genshin", "가슴", "https://gall.dcinside.com/x?no=1", "디시 개념글", "community")
        b = radar.Item("genshin", "가슴", "https://gall.dcinside.com/x?no=2", "디시 개념글", "community")
        c = radar.Item("hsr", "가슴", "https://gall.dcinside.com/x?no=1", "디시 개념글", "community")
        self.assertEqual(len({a.key, b.key, c.key}), 3)
        n1 = radar.Item("genshin", "원신 7.2 출시", "https://a.com/1", "A")
        n2 = radar.Item("genshin", "[속보] 원신 7.2 출시", "https://b.com/2", "B")
        self.assertEqual(n1.key, n2.key)

    def test_translate_fallback(self):
        # 첫 번째 번역 주소가 막히면 다음 주소로
        def fg(url, *a, **k):
            if "translate.googleapis.com" in url or "translate.google.com" in url:
                return None
            if "clients5.google.com" in url:
                return json.dumps(["번역됨"]).encode()
            return fake_get(url, *a, **k)
        radar.http_get = fg
        radar._blocked.clear()
        self.assertEqual(radar.translate_ko("Hello world", {}), "번역됨")

    def test_store_migration_and_url_filter(self):
        # 예전 키로 저장된 글 → 새 키로 바뀌고 중복 없이 유지
        d = self.tmp / "site" / "data"
        d.mkdir(parents=True)
        old = {"id": "oldkey", "game": "genshin", "kind": "community", "title": "원신 7.3 신캐 스킬 정리",
               "url": "https://gall.dcinside.com/mgallery/board/view/?id=onshinproject&no=2",
               "source": "디시 개념글", "first_seen": (KNOW - timedelta(hours=1)).isoformat(timespec="seconds"), "score": 1,
               "excerpt": "본문 https://x.com/a 끝"}
        (d / "items.json").write_text(json.dumps({"items": [old]}), "utf-8")
        radar.run_once(self.cfg, self.tmp)
        items = [i for i in self.items() if i["title"] == "원신 7.3 신캐 스킬 정리"]
        self.assertEqual(len(items), 1)                     # 같은 글로 인식 → 새 소식으로 중복 안 됨
        self.assertNotEqual(items[0]["id"], "oldkey")
        self.assertEqual(items[0]["excerpt"], "본문 끝")     # 예전 미리보기도 링크 주소 정리
        # javascript: 같은 주소는 버림
        g = self.cfg["games"][1]
        bad = radar.Item("genshin", "원신 이상한 링크", "javascript:alert(1)", "뉴스")
        self.assertFalse(bad.url.startswith(("https://", "http://")))

    def test_protect_terms(self):
        q, m = radar.protect_terms("About Raiden in 7.2 Genshin Impact", self.cfg["glossary"])
        self.assertNotIn("Raiden", q)
        self.assertNotIn("Genshin", q)
        out = radar.restore_terms(q.replace("About", "소개:"), m)
        self.assertEqual(out, "소개: 라이덴 in 7.2 원신")
        self.assertEqual(radar.restore_terms("zqx0z 에 대해", {"ZQX0Z": "라이덴"}), "라이덴 에 대해")

    def test_digest_keeps_coverage(self):
        it = radar.dict_to_item({"game": "genshin", "title": "t", "url": "https://a", "source": "A", "coverage": 3})
        self.assertEqual(it.coverage, 3)

    def test_telegram_html_is_escaped(self):
        it = radar.Item("genshin", "<script>&", "https://ex.com/?a=1&b=2", "src")
        s = radar.fmt_item(it, datetime.now(timezone.utc), "html")
        self.assertIn("&lt;script&gt;&amp;", s)


if __name__ == "__main__":
    unittest.main()
