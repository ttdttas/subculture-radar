# 🎮 서브컬쳐 레이더

블루 아카이브 · 원신 · 젠레스 존 제로 · 붕괴: 스타레일 · 명조의
**새 떡밥 / 공식 소식 / 커뮤니티 화제 / 많이 찾는 검색어**를 매일 아침·점심·저녁 자동으로 모아서
디스코드나 텔레그램으로 보내 주는 프로그램입니다.

- Python 3.11 이상, **추가 설치 필요 없음** (기본 라이브러리만 사용)
- 무료로 **GitHub Actions**에서 돌리거나, 내 PC/서버에서 켜 둘 수 있음

---

## 무엇을 어디서 모으나

| 구분 | 출처 | 설명 |
|---|---|---|
| 📢 공식 / 뉴스 | 구글 뉴스 RSS (한국어 + 영어), 네이버 뉴스 API(선택) | 제목에 게임 이름이 있는 기사만. 여러 매체가 다룬 소식은 하나로 합치고 "매체 N곳"으로 표시 |
| 🕵️ 떡밥·유출 | Reddit 유출 게시판 (`Genshin_Impact_Leaks`, `HonkaiStarRail_leaks`, `Zenlesszonezeroleaks_`, `WutheringWavesLeaks`) + "유출/루머/베타" 키워드 | 게시판 플레어(Reliable, Datamined, Questionable…)로 **신뢰도** 표시 |
| 🔥 화제 | Reddit 공식 게시판 인기글, 구글 트렌드 급상승 검색어(한국·미국·일본) | 추천수·댓글수로 점수 |
| 🔍 많이 찾는 검색어 | 구글 자동완성 | 사람들이 실제로 검색하는 조합 (예: "원신 7.1 픽업"). 지난 회차에 없던 건 **🆕 새로 뜬 검색어**로 따로 표시 |
| (선택) 공식 영상 | 유튜브 채널 RSS | `config.toml`에 채널 ID를 넣으면 새 영상 알림 |

같은 소식은 7일 동안 기억해서 **두 번 알리지 않습니다** (`state/seen.json`).
매 회차 결과는 `reports/연/월/일-시분.md`, 가장 최근 결과는 `reports/latest.md`에 저장됩니다.

---

## 방법 1. GitHub Actions로 자동 실행 (추천 · 무료 · PC 꺼져 있어도 됨)

1. GitHub에서 새 저장소를 만들고(비공개도 됨) 이 폴더의 파일을 전부 올립니다.
   `.github/workflows/radar.yml`도 꼭 같이 올려야 합니다.
2. **알림 받을 곳 만들기** (하나만 해도 됨)
   - **디스코드**: 채널 설정 → 연동 → 웹후크 → 새 웹후크 → *웹후크 URL 복사*
   - **텔레그램**: `@BotFather`에게 `/newbot` → 받은 토큰 저장. 만든 봇에게 아무 말이나 보낸 뒤
     `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어서 `"chat":{"id":...}` 숫자를 확인
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret** 에 등록:

   | 이름 | 값 |
   |---|---|
   | `DISCORD_WEBHOOK_URL` | 디스코드 웹후크 URL |
   | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 텔레그램 토큰 / 채팅 ID |
   | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | **강력 추천** — 아래 참고 |
   | `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 선택 — 국내 기사 보강 |

4. **Actions** 탭 → "서브컬쳐 레이더" → **Run workflow** 로 한 번 수동 실행해서 알림이 오는지 확인.
   이후로는 매일 **07:50 / 11:50 / 18:50 (한국시간)** 에 알아서 돌아갑니다.
   (GitHub 사정으로 몇 분~십몇 분 늦게 시작될 수 있어요.)

### Reddit 키를 넣어야 하는 이유
Reddit은 GitHub 같은 클라우드 서버에서 오는 요청을 자주 막습니다(403/429).
키 없이도 RSS로 우회는 하지만, 그러면 **추천수·플레어(신뢰도) 정보가 빠지고 일부 게시판은 누락**될 수 있어요.

1. 로그인 후 <https://www.reddit.com/prefs/apps> → **create another app…**
2. 이름 아무거나, 종류는 **script**, redirect uri 는 `http://localhost:8080`
3. 앱 이름 아래 짧은 문자열 = `REDDIT_CLIENT_ID`, `secret` = `REDDIT_CLIENT_SECRET`

### 네이버 뉴스 API (선택)
<https://developers.naver.com/apps> → 애플리케이션 등록 → 사용 API에 **검색** 선택 → Client ID / Secret.

---

## 방법 2. 내 PC나 서버에서 실행

```bash
# 테스트 (전송·저장 없이 결과만 화면에 출력)
python radar.py --dry-run

# 한 번 실행 (알림 전송 + 기록 저장)
python radar.py

# 켜 두면 config.toml 의 schedule 시각마다 계속 실행
python radar.py --loop
```

알림을 보내려면 실행 전에 환경 변수를 설정하세요.

```bash
# macOS / Linux
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
# Windows PowerShell
$env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

`--loop` 대신 OS 예약 기능을 써도 됩니다.
- **Windows 작업 스케줄러**: 기본 작업 만들기 → 매일 → 프로그램 `python`, 인수 `radar.py`, 시작 위치는 이 폴더. 시간대별로 3개 만들기
- **Linux/macOS cron**: `50 7,11,18 * * * cd /경로/subculture-radar && /usr/bin/python3 radar.py`

---

## 내 입맛대로 바꾸기 (`config.toml`)

- **게임 추가**: `[[games]]` 블록을 하나 복사해서 이름·검색어·서브레딧만 바꾸면 끝 (예: 붕괴3rd, 니케, 트릭컬)
- **실행 시각**: `schedule` (내 PC `--loop`용) / `.github/workflows/radar.yml` 의 `cron` (GitHub용, UTC 기준이라 한국시간 −9시간)
- **분류 키워드**: `[keywords]` 의 `leak` / `official` / `hot` 에 단어 추가
- **거르기**: `exclude` — 팬아트·코스프레·밈 글을 보고 싶으면 해당 단어를 지우세요
- **표시 개수**: `max_per_section`, **새 소식 기준 시간**: `lookback_hours`

테스트: `python -m unittest discover -s tests -v` (인터넷 없이 가짜 데이터로 전체 흐름 검사)

---

## 알아둘 점
- "많이 검색되는 것"은 **구글 자동완성 + 구글 트렌드 급상승**으로 추정합니다. 정확한 검색량 숫자는 트렌드에 뜬 항목만 나옵니다.
  정확한 국내 검색량 추이가 필요하면 네이버 데이터랩 API를 추가로 붙이면 됩니다.
- 구글 자동완성·트렌드 RSS는 공식 API가 아니라 언젠가 형식이 바뀔 수 있습니다. 한 소스가 실패해도 나머지는 계속 수집됩니다.
- 아카라이브·디시인사이드는 봇 접근을 막고 있어서 넣지 않았습니다.
- 유출 정보는 말 그대로 **확인 안 된 정보**입니다. 신뢰도 표시는 Reddit 게시판 플레어 기준입니다.
- 주말·새벽에는 국내 기사가 거의 없어서 "새 소식 없음"이 자주 뜰 수 있어요.
