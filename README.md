# KBO 로스터 트래커 (kbo-roster)

KBO 선수들의 1군 등록 / 2군 말소 / 부상 등 상태 변화를 추적하는 Django 웹 애플리케이션.
KBO 공식 사이트에서 등록/말소 현황을 매일 자동으로 수집하고, 사유(부상 부위 등)는 추후 각 구단
SNS·미디어를 참조해 반영할 수 있는 구조로 되어 있다. 1군/2군(북부·남부) 리그 순위, 구장별 좌석
매진율 통계도 KBO 공식 사이트에서 실시간으로 가져와 보여주고, 선수 관련 최신 뉴스는 네이버 뉴스
검색 API로 수집해 Groq 호스팅 오픈소스 LLM이 요약해준다.

MIT 라이선스로 공개되어 있으며 누구나 자유롭게 PR을 보낼 수 있다 — 기여 방법은
[CONTRIBUTING.md](CONTRIBUTING.md) 참고. 실제 서비스: https://kbo-roster.onrender.com

## 기술 스택

- Python 3.11 / Django 5.2 (서버 렌더링 템플릿)
- SQLite(로컬 개발용) / PostgreSQL(배포용, `DATABASE_URL` 환경변수로 자동 전환)
- requests + BeautifulSoup(lxml) — KBO 공식 사이트 스크래핑
- 네이버 뉴스 검색 API — 선수 관련 기사 수집
- Groq API(오픈소스 모델 `openai/gpt-oss-120b` 호스팅) — 수집된 기사 요약/설명 생성
- python-dotenv — `.env` 파일로 API 키 등 민감 설정 분리
- gunicorn + whitenoise — 배포 환경 WSGI 서버 및 정적 파일 서빙

## 프로젝트 구조

```
kbo-roster/
├── config/                  # Django 프로젝트 설정 (settings, urls)
├── roster/                  # 메인 앱
│   ├── models.py             # Team, Player, RosterEvent
│   ├── scraping.py           # KBO 공식 사이트(RegisterAll.aspx, TeamRank.aspx, GraphDaily.aspx 등) 파서
│   ├── news.py               # 네이버 뉴스 검색 API로 선수 관련 기사 수집
│   ├── llm.py                 # Groq API 호출 — 기사 목록 기반 요약 생성
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
  - `reason`, `note`: 부상 부위 등 KBO 공식 페이지에 없는 사유. 현재는 관리자 기능이 없어
    Django 쉘(`python manage.py shell`)로 직접 기재하며, 추후 미디어/SNS 수집 파이프라인이
    채워 넣을 수 있도록 필드를 분리해 두었다.
  - `source`: `SCRAPER`(KBO 공식 자동 수집) / `MANUAL`(수동 입력) / `MEDIA`(미디어·SNS 수집)
  - `source_name`, `source_url`: 사유의 출처(예: "한화 이글스 공식 SNS")와 원문 링크.
    구단 SNS 등 미디어 기반 수집을 붙일 때, 새 스크래퍼가 이 두 필드와 `source=MEDIA`,
    `reason`을 채워 `RosterEvent`를 생성/갱신하기만 하면 기존 모델·화면을 그대로 재사용할 수
    있다. (현재는 이 수집기 자체는 구현되어 있지 않고, 확장 지점만 마련한 상태.)

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
- `roster/llm.py`의 `summarize_player_news`가 위 기사 목록만 프롬프트에 담아 Groq API
  (`openai/gpt-oss-120b`, OpenAI 호환 `/chat/completions` 엔드포인트)에 요청,
  "기사에 없는 내용은 추측하지 말 것"을 시스템 프롬프트로 명시해 사실 기반 3~5문장 요약을
  생성한다. 출처가 불분명한 SNS/커뮤니티가 아니라 뉴스 API 결과만 입력으로 쓰기 때문에,
  LLM이 근거 없는 내용을 지어낼 여지를 최소화했다.
- 선수 상세 페이지(`/players/<id>/`)의 "📰 관련 뉴스/이슈 보기" 버튼을 누르면 별도 페이지
  (`/players/<id>/news/`)에서 AI 요약과 원문 기사 링크 목록을 보여준다.
- 원래는 로컬 Ollama(`qwen2.5:7b-instruct`)로 구현했으나, 배포 환경에는 상시 로컬 LLM 서버를
  둘 수 없어 Groq 호스팅 API로 교체했다 — 코드 구조(기사 목록만 근거로 요약, 사실 아닌 내용
  추측 금지)는 동일하게 유지했다.

## 마이팀 실시간 알림 (Web Push)

- 구단 상세 페이지(`/teams/<id>/`)에서 "🔔 이 팀 알림 받기"를 누르면 브라우저가 Web Push를
  구독하고, `PushSubscription`(팀, endpoint, p256dh/auth 키)으로 저장된다.
- `sync_roster`가 새 `RosterEvent`(1군 등록/2군 말소)를 생성할 때마다 `roster/push.py`의
  `send_push_to_team`이 해당 팀 구독자 전원에게 VAPID 서명된 Web Push 알림을 보낸다
  (`pywebpush`). 구독이 만료(404/410)되면 자동으로 `PushSubscription`을 삭제한다.
- 서비스워커(`/sw.js`)는 스코프를 사이트 전체(`/`)로 주기 위해 정적 파일이 아닌 루트 경로
  뷰(`views.service_worker`)로 서빙한다. `manifest.json` + 아이콘을 추가해 "홈 화면에 추가"도
  가능하다.
- 안드로이드(Chrome)는 설치 여부와 무관하게 거의 완벽히 동작하고, iOS는 16.4 이상에서 Safari로
  "홈 화면에 추가"한 PWA 상태에서만 푸시가 온다 (일반 브라우저 탭에서는 iOS가 지원하지 않음).
- 환경변수: `VAPID_PRIVATE_KEY_B64`(PEM을 base64로 감싼 값), `VAPID_PUBLIC_KEY`(URL-safe
  base64), `VAPID_CLAIM_EMAIL`(`mailto:...`). 새로 발급하려면:
  ```python
  from py_vapid import Vapid
  from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
  import base64

  v = Vapid(); v.generate_keys()
  print(base64.b64encode(v.private_pem()).decode())  # VAPID_PRIVATE_KEY_B64
  print(base64.urlsafe_b64encode(
      v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
  ).rstrip(b"=").decode())  # VAPID_PUBLIC_KEY
  ```

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

로컬 서버에서는 `scripts/sync_roster.sh`(가상환경 활성화 → 의존성 설치(idempotent) →
`sync_roster` 실행 → `logs/sync_roster.log`에 결과 기록)를 crontab에 등록해 매일 자동
실행할 수 있다.

배포 환경(Render)에서는 `GET /sync/?token=...` 엔드포인트(`roster/views.py`의
`trigger_sync`)를 통해 동기화한다. Render의 자체 Cron Job은 Web Service 요금제에
포함되지 않는 별도 유료 서비스(서비스당 월 최소 $1)라서, 대신 **cron-job.org** 같은
무료 외부 크론 서비스가 매일 오후 5시에 이 URL을 호출하도록 등록해두는 방식을 쓴다.

- 인증: 쿼리 파라미터 `token`이 환경변수 `SYNC_SECRET_TOKEN`과 일치해야 한다(`hmac.compare_digest`로
  비교). 토큰이 비어 있거나 틀리면 403을 반환한다.
- 하는 일: `sync_roster.py`와 동일 — 1군 등록/말소 현황만 가져와 `RosterEvent`를 갱신한다.
  경기 결과, 리그 순위, 매진율 통계는 이 크론과 무관하게 아래 캐시 전략으로 처리된다.

## 캐시 전략

KBO 공식 사이트를 매 요청마다 긁으면 응답이 느려지고 상대 서버에도 부담을 주므로, 자주 안
바뀌는 데이터는 `django.core.cache`(기본 in-memory 캐시)에 담아두고 재사용한다
(`roster/views.py`의 `_cached_fetch`). 데이터 성격에 따라 유지시간을 다르게 준다.

| 데이터 | 캐시 키 | 유지시간 | 이유 |
|---|---|---|---|
| 리그 순위(1군/2군), 좌석 매진율 통계 | `standings_1gun` 등 | 24시간 | 하루 1회 정도만 바뀌면 충분 |
| **직전 경기 결과** (`latest_game_results`) | `latest_game_results` | **15분** | KBO 경기는 보통 밤 11시 전에 끝나는데, 24시간 캐시를 쓰면 마지막으로 캐시가 채워진 시각 기준으로 다음날 같은 시각까지 어제 결과가 남아있을 수 있어 훨씬 짧게 잡았다 |

캐시는 백그라운드에서 주기적으로 도는 작업이 아니라 **요청이 들어올 때만** 동작하는
지연(lazy) 캐시다 — 캐시가 비어 있거나 유지시간이 지난 상태에서 방문자가 들어오면 그
요청 처리 중에 한 번 새로 긁어와 캐시를 채우고, 그 전까지는 캐시된 값을 그대로 재사용한다.
따라서 트래픽이 없는 시간에는 KBO 사이트에 아무 요청도 나가지 않는다.

## 대시보드 (웹 화면)

- `/` — 구단별 카드 뷰: 팀별 1군 등록 인원과 목록 (구단 로고 표시), 직전 경기일 결과
- `/teams/<id>/` — 팀 상세: 1군 등록 / 2군·기타 인원 목록
- `/players/<id>/` — 선수 상세: 현재 상태 + 전체 이력 타임라인(사유·출처 링크 포함)
- `/players/<id>/news/` — 선수 관련 최신 뉴스 + AI 요약
- `/standings/` — 리그 순위: 1군 전체 순위(포스트시즌 진출 표기 포함) + 2군 퓨처스 북부/남부 순위
- `/attendance/` — 구단별 좌석 매진율 통계: 팀별 전체 평균, 요일별/상대구단별 평균 매진율
- `/events/` — 최근 등록/말소 변동 최신 100건

Django 관리자 화면(`/admin/`)은 사용하지 않는다 — 데이터 수동 조작이 필요하면
`python manage.py shell`로 직접 처리한다.

## 로컬 실행

```bash
cd ~/kbo-roster
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py sync_roster       # 최초 데이터 수집
python manage.py runserver
```

선수 뉴스 요약 기능까지 쓰려면 프로젝트 루트에 `.env` 파일을 만들어 아래 값을 채운다
(`.env`는 `.gitignore`에 포함되어 git에는 올라가지 않는다):

```
NAVER_CLIENT_ID=발급받은_클라이언트_ID
NAVER_CLIENT_SECRET=발급받은_시크릿
GROQ_API_KEY=발급받은_Groq_API_키
```

- 네이버 API 키: [네이버 개발자센터](https://developers.naver.com)에서 애플리케이션 등록 후 발급
- Groq API 키: [console.groq.com](https://console.groq.com)에서 무료 가입 후 발급

`.env`가 없거나 키가 비어 있어도 다른 기능(리그 순위, 매진율 통계 등)은 정상 동작하며, 뉴스
페이지만 요약 없이 기사 목록만 보여준다.

## 배포 (Render / Railway 등 PaaS)

이 앱은 `DEBUG`/`SECRET_KEY`/`ALLOWED_HOSTS`/`DATABASE_URL` 등을 전부 환경변수로 읽도록
되어 있어 별도 코드 수정 없이 PaaS에 올릴 수 있다.

**필요한 환경변수:**

| 변수 | 값 | 비고 |
|---|---|---|
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(50))"`로 생성 | 로컬 개발용 키 그대로 쓰지 말 것 |
| `DEBUG` | `False` | 배포 환경에서는 반드시 False |
| `ALLOWED_HOSTS` | 예: `kbo-roster.onrender.com` | 콤마로 여러 개 구분 가능 |
| `CSRF_TRUSTED_ORIGINS` | 예: `https://kbo-roster.onrender.com` | 스킴(`https://`) 포함 필수 |
| `DATABASE_URL` | Render/Railway가 Postgres 애드온 생성 시 자동 주입 | 없으면 SQLite로 폴백(배포에는 비권장 — 재배포 시 데이터 유실) |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 발급받은 값 | 뉴스 수집용 |
| `GROQ_API_KEY` | 발급받은 값 | AI 요약용 |
| `EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | 예: `smtp.naver.com` / 네이버 계정 / 앱 비밀번호 | "의견 보내기" 메일 발송용. 비어 있으면 콘솔에만 출력되고 실제 발송은 안 됨 |
| `EMAIL_PORT` / `EMAIL_USE_TLS` | 기본값 `587` / `True` | 대부분의 SMTP(네이버, Gmail 등)에서 기본값 그대로 사용 가능 |
| `FEEDBACK_RECIPIENT_EMAIL` | 기본값 `zang03@naver.com` | 의견을 받을 주소. 바꾸고 싶을 때만 설정 |
| `VAPID_PRIVATE_KEY_B64` / `VAPID_PUBLIC_KEY` / `VAPID_CLAIM_EMAIL` | 위 "마이팀 실시간 알림" 절 참고 | 마이팀 Web Push 발송용. 비어 있으면 알림 버튼이 표시되지 않음 |

네이버 메일을 발송 계정으로 쓰려면 네이버 메일 환경설정 > POP3/IMAP 설정에서 "SMTP 사용"을
켜야 하고, 2단계 인증을 쓰는 계정이면 계정 비밀번호 대신 발급받은 앱 비밀번호를
`EMAIL_HOST_PASSWORD`에 넣는다.

**배포 절차 (Render 기준):**
1. GitHub 저장소(`main` 브랜치)를 Render에 연결해 새 Web Service 생성
2. Build Command: `pip install -r requirements.txt && python manage.py collectstatic --noinput`
3. Start Command: `gunicorn config.wsgi --log-file -` (`Procfile`에도 동일하게 정의되어 있음)
4. 위 표의 환경변수를 모두 등록
5. Postgres 인스턴스를 하나 추가로 만들어 `DATABASE_URL`을 연결(SQLite는 배포 파일시스템이
   재시작/재배포마다 초기화되는 PaaS 특성상 데이터가 날아갈 수 있어 배포에는 적합하지 않음)

Railway도 동일한 방식이며, `Procfile`의 `release:`/`web:` 커맨드를 그대로 인식한다.

## 알려진 제약 / 향후 과제

- 선수 식별이 이름 기반이라, 동명이인이 실제로 발생하면 Django 쉘에서 수동으로 분리해야 한다.
- 관리자 화면(`django.contrib.admin`)은 현재 필요 없어 제거했다 — 데이터 CRUD가 다시
  필요해지면 `INSTALLED_APPS`에 `'django.contrib.admin'`을 되돌리고 `config/urls.py`에
  `path('admin/', admin.site.urls)`를, `roster/admin.py`에 모델 등록을 다시 추가하면 된다.
- 부상 부위 등 상세 사유는 아직 자동 수집기가 없다 — 구단 SNS/미디어 파서를 추가할 때는
  `RosterEvent(source=RosterEvent.SOURCE_MEDIA, reason=..., source_name=..., source_url=...)`
  형태로 채워 넣으면 기존 화면·모델 변경 없이 바로 반영된다.
- 트레이드로 팀이 바뀌는 경우 `Player.team`은 최신 값으로 갱신되지만, 과거 각 `RosterEvent`에는
  당시 소속팀이 그대로 남아 있어 이력 조회 시 참고할 수 있다.
- 직전 경기일 결과(`/`에 표시)는 구현되어 있으나, 시즌 전체 일정/과거 경기 이력 조회 페이지는
  아직 없다 — 다음 작업으로 예정.
- 선수 뉴스 AI 요약은 네이버 뉴스 검색 결과에 잡히는 기사만 근거로 삼는다. 검색 결과 자체가
  부실하거나(동명이인 뉴스 혼입 등) 편향된 경우 요약 품질도 그대로 영향을 받는다 — 화이트리스트
  기반 출처 필터링 등은 아직 없다.
- 등록/말소 사유에 대한 LLM 추론(성적·부상이력·수비지표 기반 기대이점 예측)은 아직 구현하지
  않았다 — 정량 예측은 별도 통계 모델로, LLM은 결과 설명만 맡기는 구조로 설계할 예정.

## 기여 / 라이선스

PR과 이슈 제안을 환영한다. 기여 방법과 브랜치 전략은 [CONTRIBUTING.md](CONTRIBUTING.md)를
참고. 이 프로젝트는 [MIT 라이선스](LICENSE)로 배포된다.
