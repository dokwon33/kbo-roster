# KBO 로스터 트래커 (kbo-roster)

KBO 선수들의 1군 등록 / 2군 말소 / 부상 등 상태 변화를 추적하는 Django 웹 애플리케이션.
KBO 공식 사이트에서 등록/말소 현황을 매일 자동으로 수집하고, 사유(부상 부위 등)는 추후 각 구단
SNS·미디어를 참조해 반영할 수 있는 구조로 되어 있다. 1군/2군(북부·남부) 리그 순위, 구장별 좌석
매진율 통계도 KBO 공식 사이트에서 실시간으로 가져와 보여주고, 선수 관련 최신 뉴스는 네이버 뉴스
검색 API로 수집해 로컬 LLM이 요약해준다.

## 기술 스택

- Python 3.11 / Django 5.2 (서버 렌더링 템플릿)
- SQLite (개발용 기본 DB)
- requests + BeautifulSoup(lxml) — KBO 공식 사이트 스크래핑
- 네이버 뉴스 검색 API — 선수 관련 기사 수집
- Ollama(로컬 오픈소스 LLM, `qwen2.5:7b-instruct`) — 수집된 기사 요약/설명 생성
- python-dotenv — `.env` 파일로 API 키 등 민감 설정 분리

## 프로젝트 구조

```
kbo-roster/
├── config/                  # Django 프로젝트 설정 (settings, urls)
├── roster/                  # 메인 앱
│   ├── models.py             # Team, Player, RosterEvent
│   ├── scraping.py           # KBO 공식 사이트(RegisterAll.aspx, TeamRank.aspx, GraphDaily.aspx 등) 파서
│   ├── news.py               # 네이버 뉴스 검색 API로 선수 관련 기사 수집
│   ├── llm.py                 # 로컬 Ollama 호출 — 기사 목록 기반 요약 생성
│   ├── admin.py              # 관리자 화면 (선수/이벤트 CRUD, 사유 수동 기재)
│   ├── views.py / urls.py    # 대시보드 뷰
│   ├── templates/roster/     # 팀별 현황, 리그 순위, 매진율 통계, 선수 상세/뉴스, 최근 변동 페이지
│   ├── templatetags/         # 상태 뱃지 · 구단 로고 · 연속 승패 표시용 템플릿 필터
│   └── management/commands/
│       └── sync_roster.py    # 스크래핑 결과를 DB에 반영하는 커맨드
├── scripts/sync_roster.sh    # 크론에서 호출하는 실행 스크립트
├── logs/sync_roster.log      # 동기화 실행 로그
└── requirements.txt
```

## 데이터 모델

- **Team**: 구단명
- **Player**: 이름, 소속 팀, 포지션, 등번호. 이름만으로 식별한다 (KBO 사이트가 등록/말소 현황
  페이지에서 선수 고유 ID를 제공하지 않고, 동일 팀 내 동명이인 가능성은 낮다고 보고 단순화했다).
  `kbo_player_id`, `birth_date`, `photo_url`은 신규 선수가 생성될 때 KBO 선수 조회 페이지
  (`Player/Search.aspx`)에서 자동으로 채워진다. 사진은 다운로드해 저장하지 않고 KBO CDN의
  URL만 저장한다 — 용량 부담도 없고 재호스팅 문제도 없다.
- **RosterEvent**: 선수별 상태 변화 이력. 한 선수가 여러 건을 가질 수 있고, 최신 `event_date`
  기준 레코드가 "현재 상태"(`Player.current_status`)가 된다.
  - `event_type`: 1군 등록 / 2군 말소 / 부상자 명단 / 군 입대·전역 / 출장 정지 / 방출·은퇴 / 기타
  - `reason`, `note`: 부상 부위 등 KBO 공식 페이지에 없는 사유. 현재는 관리자 화면에서 수동
    기재하며, 추후 미디어/SNS 수집 파이프라인이 채워 넣을 수 있도록 필드를 분리해 두었다.
  - `source`: `SCRAPER`(KBO 공식 자동 수집) / `MANUAL`(수동 입력) / `MEDIA`(미디어·SNS 수집)
  - `source_name`, `source_url`: 사유의 출처(예: "한화 이글스 공식 SNS")와 원문 링크.
    구단 SNS 등 미디어 기반 수집을 붙일 때, 새 스크래퍼가 이 두 필드와 `source=MEDIA`,
    `reason`을 채워 `RosterEvent`를 생성/갱신하기만 하면 기존 모델·화면·관리자 UI를 그대로
    재사용할 수 있다. (현재는 이 수집기 자체는 구현되어 있지 않고, 확장 지점만 마련한 상태.)

## 데이터 수집 (스크래핑)

- 대상: `https://www.koreabaseball.com/Player/RegisterAll.aspx`
- 이 페이지는 ASP.NET WebForms 기반이라 날짜 이동이 `__doPostBack`을 통한 viewstate POST로
  동작한다. `roster/scraping.py`는 두 가지 방식을 제공한다.
  - `fetch_current()`: 별도 조작 없이 GET 요청 — 사이트가 기본으로 보여주는 "현재 기준일"
    데이터를 가져온다. 매일 자동 동기화에는 이 함수를 사용한다.
  - `fetch_for_date(date)`: viewstate/이벤트 검증 토큰을 읽어 특정 날짜를 조회하는 POST를
    재현한다. 과거 특정일 백필이 필요할 때 사용.
- 페이지 내 "1군 등록 현황"(`div.fistStatus`) / "1군 말소 현황"(`div.fistCancelStatus`) 두
  테이블에서 선수명·포지션·소속팀을 파싱한다.
- 리그 순위는 `Record/TeamRank/TeamRank.aspx`(1군), `Futures/TeamRank/North.aspx`·`South.aspx`
  (2군 퓨처스 북부/남부)를 그때그때 실시간으로 조회한다 — DB에 저장하지 않고 `/standings/`
  요청이 올 때마다 파싱해서 보여준다(선수 시즌 성적 조회와 동일한 방식).
- 구단 로고는 KBO 이미지 CDN의 엠블럼 URL(`emblemBF_{팀코드}.png`)을 팀명→코드 매핑으로
  조합해 사용한다 (`roster/templatetags/roster_extras.py`의 `team_logo_url` 필터). 1군 10개
  구단 외에 2군 전용 팀명(고양→키움 2군, 상무, 울산)도 매핑해 두었다.
- 구장별 좌석 매진율은 `Record/Crowd/GraphDaily.aspx`(일자별 관중 현황: 날짜/요일/홈/원정/구장/
  관중수)에서 시즌 전체 데이터를 한 번에 가져온 뒤, `scraping.STADIUM_CAPACITY`에 직접 정리해둔
  구장별 좌석 수(관중수 ÷ 좌석 수)로 매진율을 계산한다. KBO가 좌석 수 자체는 제공하지 않기 때문에
  정적 테이블로 관리하며, 삼성이 가끔 치르는 포항 경기처럼 이 표에 없는 구장은 자동으로 집계에서
  제외된다.

## 선수 관련 뉴스 · AI 요약

- `roster/news.py`의 `fetch_player_news(player_name)`이 네이버 뉴스 검색 API
  (`openapi.naver.com/v1/search/news.json`)로 선수 이름을 최신순 검색해 기사 목록(제목/요약/
  링크/날짜)을 가져온다. API 키(`NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`)는 `.env`에 보관하고
  `config/settings.py`가 `python-dotenv`로 읽어들인다 (`.env`는 git에 커밋되지 않음).
- `roster/llm.py`의 `summarize_player_news`가 위 기사 목록만 프롬프트에 담아 로컬 Ollama
  (`qwen2.5:7b-instruct`)에 요청, "기사에 없는 내용은 추측하지 말 것"을 명시해 사실 기반 3~5문장
  요약을 생성한다. 출처가 불분명한 SNS/커뮤니티가 아니라 뉴스 API 결과만 입력으로 쓰기 때문에,
  LLM이 근거 없는 내용을 지어낼 여지를 최소화했다.
- 선수 상세 페이지(`/players/<id>/`)의 "📰 관련 뉴스/이슈 보기" 버튼을 누르면 별도 페이지
  (`/players/<id>/news/`)에서 AI 요약과 원문 기사 링크 목록을 보여준다. LLM 응답에 몇 초에서
  십수 초가 걸릴 수 있어 선수 상세 페이지 로딩과는 분리했다.
- 로컬 실행 시 Ollama가 떠 있어야 한다: `brew services start ollama` (또는
  `ollama serve`) 후 `ollama pull qwen2.5:7b-instruct`.

## 동기화 커맨드 & diff 로직

```
python manage.py sync_roster              # 사이트의 현재 기준일 데이터로 동기화
python manage.py sync_roster --date 20260721  # 특정 날짜 기준으로 동기화
```

동작 방식:
1. 스크래핑 결과의 각 선수에 대해 `Team`/`Player`를 없으면 생성한다.
2. 선수의 마지막 `RosterEvent`와 비교해 상태(`event_type`) 또는 소속팀이 바뀐 경우에만 새
   `RosterEvent`(`source=SCRAPER`)를 생성한다 — 변화가 없으면 아무 것도 쓰지 않는다.
3. `Player.team`/`position`은 최신 값으로 갱신한다.

## 자동 실행 (cron)

`scripts/sync_roster.sh`가 가상환경 활성화 → 의존성 설치(idempotent) → `sync_roster` 실행 →
`logs/sync_roster.log`에 결과를 남기는 역할을 한다. 매일 09:00(KST)에 실행되도록 이미
crontab에 등록되어 있다:

```
0 9 * * * /Users/ldk/kbo-roster/scripts/sync_roster.sh
```

등록 확인/수정: `crontab -l` / `crontab -e`. 시간을 바꾸고 싶으면 이 한 줄만 수정하면 된다.

## 대시보드 (웹 화면)

- `/` — 구단별 카드 뷰: 팀별 1군 등록 인원과 목록 (구단 로고 표시), 직전 경기일 결과
- `/teams/<id>/` — 팀 상세: 1군 등록 / 2군·기타 인원 목록
- `/players/<id>/` — 선수 상세: 현재 상태 + 전체 이력 타임라인(사유·출처 링크 포함)
- `/players/<id>/news/` — 선수 관련 최신 뉴스 + AI 요약
- `/standings/` — 리그 순위: 1군 전체 순위(포스트시즌 진출 표기 포함) + 2군 퓨처스 북부/남부 순위
- `/attendance/` — 구단별 좌석 매진율 통계: 팀별 전체 평균, 요일별/상대구단별 평균 매진율
- `/events/` — 최근 등록/말소 변동 최신 100건
- `/admin/` — Django 관리자: 선수/이벤트 CRUD, 사유·출처 수동 기재

## 로컬 실행

```bash
cd ~/kbo-roster
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser   # 관리자 계정이 아직 없다면
python manage.py sync_roster       # 최초 데이터 수집
python manage.py runserver
```

선수 뉴스 요약 기능까지 쓰려면 추가로:

```bash
# 프로젝트 루트에 .env 파일 생성 (git에는 안 올라감)
echo "NAVER_CLIENT_ID=발급받은_클라이언트_ID" >> .env
echo "NAVER_CLIENT_SECRET=발급받은_시크릿" >> .env

brew install ollama
brew services start ollama
ollama pull qwen2.5:7b-instruct
```

네이버 API 키는 [네이버 개발자센터](https://developers.naver.com)에서 애플리케이션을 등록하면
발급받을 수 있다. `.env`가 없거나 Ollama가 꺼져 있어도 다른 기능(리그 순위, 매진율 통계 등)은
정상 동작하며, 뉴스 페이지만 "요약을 생성하지 못했습니다"로 표시된다.

## 알려진 제약 / 향후 과제

- 선수 식별이 이름 기반이라, 동명이인이 실제로 발생하면 관리자 화면에서 수동으로 분리해야 한다.
- 부상 부위 등 상세 사유는 아직 자동 수집기가 없다 — 구단 SNS/미디어 파서를 추가할 때는
  `RosterEvent(source=RosterEvent.SOURCE_MEDIA, reason=..., source_name=..., source_url=...)`
  형태로 채워 넣으면 기존 화면·모델 변경 없이 바로 반영된다.
- 트레이드로 팀이 바뀌는 경우 `Player.team`은 최신 값으로 갱신되지만, 과거 각 `RosterEvent`에는
  당시 소속팀이 그대로 남아 있어 이력 조회 시 참고할 수 있다.
- 경기결과(시즌 전체 일정/과거 이력) 조회는 아직 구현되어 있지 않다 — 다음 작업으로 예정.
- 선수 뉴스 AI 요약은 네이버 뉴스 검색 결과에 잡히는 기사만 근거로 삼는다. 검색 결과 자체가
  부실하거나(동명이인 뉴스 혼입 등) 편향된 경우 요약 품질도 그대로 영향을 받는다 — 화이트리스트
  기반 출처 필터링 등은 아직 없다.
- 등록/말소 사유에 대한 LLM 추론(성적·부상이력·수비지표 기반 기대이점 예측)은 아직 구현하지
  않았다 — 정량 예측은 별도 통계 모델로, LLM은 결과 설명만 맡기는 구조로 설계할 예정.
