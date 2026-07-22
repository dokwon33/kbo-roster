# KBO 선수단 관리 (kbo-roster)

KBO 선수들의 1군 등록 / 2군 말소 / 부상 등 상태 변화를 추적하는 Django 웹 애플리케이션.
KBO 공식 사이트에서 등록/말소 현황을 매일 자동으로 수집하고, 사유(부상 부위 등)는 추후 각 구단
SNS·미디어를 참조해 반영할 수 있는 구조로 되어 있다.

## 기술 스택

- Python 3.11 / Django 5.2 (서버 렌더링 템플릿)
- SQLite (개발용 기본 DB)
- requests + BeautifulSoup(lxml) — KBO 공식 사이트 스크래핑

## 프로젝트 구조

```
kbo-roster/
├── config/                  # Django 프로젝트 설정 (settings, urls)
├── roster/                  # 메인 앱
│   ├── models.py             # Team, Player, RosterEvent
│   ├── scraping.py           # KBO 공식 사이트(RegisterAll.aspx) 파서
│   ├── admin.py              # 관리자 화면 (선수/이벤트 CRUD, 사유 수동 기재)
│   ├── views.py / urls.py    # 대시보드 뷰
│   ├── templates/roster/     # 팀별 현황, 선수 상세, 최근 변동 페이지
│   ├── templatetags/         # 상태 뱃지 표시용 템플릿 필터
│   └── management/commands/
│       └── sync_roster.py    # 스크래핑 결과를 DB에 반영하는 커맨드
├── scripts/sync_roster.sh    # 크론에서 호출하는 실행 스크립트
├── logs/sync_roster.log      # 동기화 실행 로그
└── requirements.txt
```

## 데이터 모델

- **Team**: 구단명
- **Player**: 이름, 소속 팀, 포지션, 등번호. 이름만으로 식별한다 (KBO 사이트가 이 페이지에서
  선수 고유 ID를 제공하지 않고, 동일 팀 내 동명이인 가능성은 낮다고 보고 단순화했다).
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

- `/` — 구단별 카드 뷰: 팀별 1군 등록 인원과 목록
- `/teams/<id>/` — 팀 상세: 1군 등록 / 2군·기타 인원 목록
- `/players/<id>/` — 선수 상세: 현재 상태 + 전체 이력 타임라인(사유·출처 링크 포함)
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

## 알려진 제약 / 향후 과제

- 선수 식별이 이름 기반이라, 동명이인이 실제로 발생하면 관리자 화면에서 수동으로 분리해야 한다.
- 부상 부위 등 상세 사유는 아직 자동 수집기가 없다 — 구단 SNS/미디어 파서를 추가할 때는
  `RosterEvent(source=RosterEvent.SOURCE_MEDIA, reason=..., source_name=..., source_url=...)`
  형태로 채워 넣으면 기존 화면·모델 변경 없이 바로 반영된다.
- 트레이드로 팀이 바뀌는 경우 `Player.team`은 최신 값으로 갱신되지만, 과거 각 `RosterEvent`에는
  당시 소속팀이 그대로 남아 있어 이력 조회 시 참고할 수 있다.
