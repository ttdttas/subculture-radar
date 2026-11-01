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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import radar  # noqa: E402

NOW = datetime.now(timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t} - {s}</title><link>{l}</link>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=h))}</pubDate><source url='x'>{s}</source></item>"
        for t, l, s, h in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


TRENDS = b"""<?xml version='1.0'?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
<item><title>\xec\x9b\x90\xec\x8b\xa0 \xec\xbd\x94\xeb\x93\x9c</title><ht:approx_traffic>20000+</ht:approx_traffic>
<pubDate>""" + format_datetime(NOW).encode() + b"""</pubDate>
<ht:news_item><ht:news_item_title>x</ht:news_item_title><ht:news_item_url>https://ex.com/t</ht:news_item_url></ht:news_item>
</item></channel></rss>"""


def fake_get(url, headers=None, data=None, timeout=20, retries=2):
    if "trends.google.com" in url:
        return TRENDS if "geo=KR" in url else None
    if "news.google.com" in url:
        if "%EC%9B%90%EC%8B%A0" in url:  # '원신'
            return rss([
                ("원신 6.2 버전 특별 방송 일정 공개", "https://ex.com/a", "인벤", 2),
                ("원신 6.2 버전 특별방송 일정 공개됐다", "https://ex.com/b", "루리웹", 3),
                ("원신 신규 캐릭터 유출 논란", "https://ex.com/c", "게임메카", 5),
                ("아주 오래된 원신 기사", "https://ex.com/old", "옛날신문", 40),
            ])
        return rss([])
    if "reddit.com" in url and "hot.json" in url:
        if "Genshin_Impact_Leaks" in url:
            return json.dumps({"data": {"children": [
                {"data": {"title": "[6.3 Beta] New 5-star kit via Dim", "permalink": "/r/x/1",
                          "ups": 4200, "num_comments": 800, "created_utc": time.time() - 3600,
                          "link_flair_text": "Reliable"}},
                {"data": {"title": "Pinned rules", "permalink": "/r/x/2", "stickied": True,
                          "ups": 1, "num_comments": 0, "created_utc": time.time()}},
            ]}}).encode()
        return json.dumps({"data": {"children": []}}).encode()
    if "suggestqueries" in url:
        q = url.split("q=")[-1]
        return json.dumps(["q", [f"sugg-{q[:6]}-1", f"sugg-{q[:6]}-2"]]).encode()
    return None


class RadarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.toml", self.tmp / "config.toml")
        self.cfg = tomllib.loads((self.tmp / "config.toml").read_text("utf-8"))
        self._orig = radar.http_get
        radar.http_get = fake_get
        radar._reddit_token = None
        radar._reddit_json_blocked = False
        radar._reddit_failures = 0
        radar.REDDIT_DELAY = 0

    def tearDown(self):
        radar.http_get = self._orig
        shutil.rmtree(self.tmp)

    def test_full_pipeline_and_dedupe(self):
        md = radar.run_once(self.cfg, self.tmp)
        print("\n" + md)
        self.assertIn("🌟 원신", md)
        self.assertIn("📢 공식", md)
        self.assertIn("[유출·신뢰도 높음]", md)          # Reddit Reliable 플레어
        self.assertIn("매체 2곳", md)                     # 비슷한 기사 2개가 합쳐짐
        self.assertNotIn("아주 오래된", md)               # 기간 밖 기사 제외
        self.assertNotIn("Pinned rules", md)              # 고정글 제외
        self.assertIn("급상승 검색어", md)                # 트렌드 매칭
        self.assertIn("🔍 많이 찾는 검색어", md)
        self.assertTrue((self.tmp / "reports" / "latest.md").exists())

        # 두 번째 실행: 같은 소식은 다시 안 나와야 함
        md2 = radar.run_once(self.cfg, self.tmp)
        self.assertIn("새 소식 0개", md2)
        self.assertNotIn("특별 방송", md2)

    def test_keyword_boundaries(self):
        pats = radar.compile_keywords(self.cfg["keywords"])
        self.assertIsNone(pats["official"].search("how to prevent this"))   # 'event' 오탐 X
        self.assertIsNotNone(pats["leak"].search("HSR leaks: new path"))
        self.assertIsNotNone(pats["exclude"].search("Hanako (art by @someone)"))
        self.assertIsNone(pats["exclude"].search("Ocean event schedule"))   # 'OC' 오탐 X

    def test_reddit_circuit_breaker(self):
        calls = []
        radar.http_get = lambda url, *a, **k: calls.append(url) or None
        g = self.cfg["games"][1]
        since = datetime.now(timezone.utc) - timedelta(hours=14)
        for sub in ["a", "b", "c", "d"]:
            self.assertEqual(radar.fetch_reddit(g, sub, since), [])
        self.assertEqual(len(calls), 3)   # a(json+rss), b(rss) 이후엔 요청 안 함

    def test_next_run(self):
        now = datetime(2026, 11, 1, 20, 0, tzinfo=radar.KST)
        self.assertEqual(radar.next_run(["07:50", "11:50", "18:50"], now).strftime("%d %H:%M"), "02 07:50")

    def test_telegram_html_is_escaped(self):
        it = radar.Item("genshin", "<script>&", "https://ex.com/?a=1&b=2", "src")
        s = radar.fmt_item(it, datetime.now(timezone.utc), "html")
        self.assertIn("&lt;script&gt;&amp;", s)


if __name__ == "__main__":
    unittest.main()
