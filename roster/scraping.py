"""KBO 공식 사이트(koreabaseball.com) 1군 등록/말소 현황 스크래퍼.

대상 페이지: https://www.koreabaseball.com/Player/RegisterAll.aspx
ASP.NET WebForms 기반이라 날짜 이동은 __doPostBack 을 통한 폼 POST(viewstate 포함)로 처리된다.
기본 GET 요청은 사이트가 판단한 "현재 기준일"의 데이터를 반환한다.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.koreabaseball.com/Player/RegisterAll.aspx"
SEARCH_URL = "https://www.koreabaseball.com/Player/Search.aspx"
PHOTO_URL_TEMPLATE = "//6ptotvmi5753.edge.naverncp.com/KBO_IMAGE/person/middle/{year}/{player_id}.jpg"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; kbo-roster-tracker/1.0)"}

DATE_LABEL_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")
PLAYER_ID_RE = re.compile(r"playerId=(\d+)")


@dataclass
class RosterRow:
    name: str
    position: str
    team: str


@dataclass
class ScrapeResult:
    as_of: date
    registered: list  # RosterRow — 1군 등록 현황
    cancelled: list  # RosterRow — 1군 말소 현황


def _parse_page(html: str) -> ScrapeResult:
    soup = BeautifulSoup(html, "lxml")

    label = soup.select_one("#cphContents_cphContents_cphContents_lblGameDate")
    m = DATE_LABEL_RE.search(label.get_text()) if label else None
    as_of = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else date.today()

    def _rows(container_class):
        container = soup.select_one(f"div.{container_class} table.tData tbody")
        rows = []
        if not container:
            return rows
        for tr in container.select("tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if len(cells) != 3:
                continue
            name, position, team = cells
            if not name:
                continue
            rows.append(RosterRow(name=name, position=position, team=team))
        return rows

    return ScrapeResult(
        as_of=as_of,
        registered=_rows("fistStatus"),
        cancelled=_rows("fistCancelStatus"),
    )


def fetch_current() -> ScrapeResult:
    """사이트가 기본으로 보여주는 최신 등록/말소 현황을 가져온다."""
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _parse_page(resp.text)


def fetch_for_date(target: date) -> ScrapeResult:
    """특정 날짜의 등록/말소 현황을 가져온다 (ASP.NET postback 시뮬레이션)."""
    session = requests.Session()
    resp = session.get(BASE_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    form_data = {}
    for hidden in soup.select("input[type=hidden]"):
        name = hidden.get("name")
        if name:
            form_data[name] = hidden.get("value", "")

    date_field = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$hfSearchDate"
    form_data[date_field] = target.strftime("%Y%m%d")
    form_data["__EVENTTARGET"] = "ctl00$ctl00$ctl00$cphContents$cphContents$cphContents$btnSearch"
    form_data["__EVENTARGUMENT"] = ""

    resp = session.post(BASE_URL, data=form_data, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _parse_page(resp.text)


@dataclass
class PlayerProfile:
    kbo_player_id: str
    birth_date: date | None
    team: str
    position: str
    photo_url: str
    back_number: str = ""


BACK_NO_RE = re.compile(r'id="[^"]*lblBackNo"[^>]*>([^<]*)<')


def fetch_back_number(player_id: str, position: str) -> str:
    """선수 상세 페이지(타자/투수)에서 등번호를 가져온다."""
    url_template = PITCHER_DETAIL_URL["1군"] if position == "투" else HITTER_DETAIL_URL["1군"]
    resp = requests.get(url_template.format(player_id=player_id), headers=HEADERS, timeout=15)
    resp.raise_for_status()
    m = BACK_NO_RE.search(resp.text)
    return m.group(1).strip() if m else ""


def resolve_player_profile(name: str, team: str | None = None) -> PlayerProfile | None:
    """이름으로 KBO 공식 선수 조회 페이지를 검색해 고유 코드/생년월일/사진 URL을 찾는다.

    동명이인이 여러 명 나오면 team이 일치하는 행을 우선한다.
    """
    resp = requests.get(SEARCH_URL, params={"searchWord": name}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    candidates = []
    for link in soup.select("a[href*='playerId=']"):
        m = PLAYER_ID_RE.search(link["href"])
        if not m:
            continue
        row = link.find_parent("tr")
        if not row:
            continue
        cells = [td.get_text(strip=True) for td in row.select("td")]
        if len(cells) < 5:
            continue
        _, player_name, row_team, position, birth_str = cells[:5]
        if player_name != name:
            continue
        candidates.append((m.group(1), row_team, position, birth_str))

    if not candidates:
        return None

    chosen = next((c for c in candidates if c[1] == team), candidates[0]) if team else candidates[0]
    player_id, row_team, position, birth_str = chosen

    birth_date = None
    try:
        birth_date = datetime.strptime(birth_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    photo_url = "https:" + PHOTO_URL_TEMPLATE.format(year=date.today().year, player_id=player_id)

    try:
        back_number = fetch_back_number(player_id, position)
    except requests.RequestException:
        back_number = ""

    return PlayerProfile(
        kbo_player_id=player_id,
        birth_date=birth_date,
        team=row_team,
        position=position,
        photo_url=photo_url,
        back_number=back_number,
    )


HITTER_DETAIL_URL = {
    "1군": "https://www.koreabaseball.com/Record/Player/HitterDetail/Basic.aspx?playerId={player_id}",
    "2군": "https://www.koreabaseball.com/Futures/Player/HitterDetail.aspx?playerId={player_id}",
}
PITCHER_DETAIL_URL = {
    "1군": "https://www.koreabaseball.com/Record/Player/PitcherDetail/Basic.aspx?playerId={player_id}",
    "2군": "https://www.koreabaseball.com/Futures/Player/PitcherDetail.aspx?playerId={player_id}",
}


def _parse_season_stats_table(html: str) -> dict | None:
    """선수 상세 페이지 상단의 '이번 시즌 성적' 표 하나를 헤더-값 딕셔너리로 파싱한다."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("div.player_records div.tbl-type02 table.tbl")
    if not table:
        return None

    headers = []
    for th in table.select("thead th"):
        link = th.find("a")
        label = link.get("title") if link and link.get("title") else th.get_text(strip=True)
        headers.append(label)

    row = table.select_one("tbody tr")
    if not row:
        return None
    cells = [td.get_text(strip=True) for td in row.select("td")]
    if len(cells) != len(headers):
        # 표에 데이터가 없는 경우 "기록이 없습니다" 형태의 colspan 행만 존재한다.
        return None

    return dict(zip(headers, cells))


def fetch_hitter_stats(player_id: str, league: str = "1군") -> dict | None:
    """선수의 해당 시즌 타자 기록(1군 또는 2군 퓨처스)을 가져온다."""
    url = HITTER_DETAIL_URL[league].format(player_id=player_id)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return _parse_season_stats_table(resp.text)


def fetch_pitcher_stats(player_id: str, league: str = "1군") -> dict | None:
    """선수의 해당 시즌 투수 기록(1군 또는 2군 퓨처스)을 가져온다."""
    url = PITCHER_DETAIL_URL[league].format(player_id=player_id)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return _parse_season_stats_table(resp.text)


def fetch_player_stats(player_id: str, position: str, league: str = "1군") -> dict | None:
    """포지션에 맞춰 타자/투수 기록 조회 함수를 골라 호출한다."""
    if position == "투":
        return fetch_pitcher_stats(player_id, league)
    return fetch_hitter_stats(player_id, league)


TEAM_RANK_URL = "https://www.koreabaseball.com/Record/TeamRank/TeamRank.aspx"
FUTURES_TEAM_RANK_URL = {
    "북부": "https://www.koreabaseball.com/Futures/TeamRank/North.aspx",
    "남부": "https://www.koreabaseball.com/Futures/TeamRank/South.aspx",
}


@dataclass
class TeamStandingRow:
    rank: str
    team: str
    games: str
    wins: str
    losses: str
    draws: str
    win_pct: str
    games_behind: str
    recent_10: str
    streak: str
    home_record: str
    away_record: str
    division: str = ""  # 2군(퓨처스)만 "북부"/"남부"로 채워진다.


def _parse_standings_table(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.tData, table.tbl")
    rows = []
    if not table:
        return rows
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.select("td")]
        if len(cells) != 12:
            continue
        rows.append(TeamStandingRow(*cells))
    return rows


def fetch_standings_1gun() -> list:
    """1군 팀 순위표를 가져온다."""
    resp = requests.get(TEAM_RANK_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _parse_standings_table(resp.text)


def fetch_standings_2gun() -> list:
    """2군(퓨처스) 북부/남부 리그 순위표를 가져온다."""
    rows = []
    for division, url in FUTURES_TEAM_RANK_URL.items():
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        for row in _parse_standings_table(resp.text):
            row.division = division
            rows.append(row)
    return rows


GAME_DATE_URL = "https://www.koreabaseball.com/ws/Main.asmx/GetKboGameDate"
GAME_LIST_URL = "https://www.koreabaseball.com/ws/Main.asmx/GetKboGameList"
GAME_CENTER_REFERER = "https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx"
GAME_SERIES_IDS = "0,1,3,4,5,6,7,9"  # 1군 정규시즌 + 시범경기 등 전체 시리즈

GAME_STATE_ENDED = "3"
GAME_STATE_CANCELLED = "4"


@dataclass
class GameResult:
    game_id: str
    away_team: str
    home_team: str
    away_score: int
    home_score: int
    state: str  # GAME_STATE_SC: "3"=경기종료, "4"=취소 등
    cancel_reason: str
    stadium: str
    win_pitcher: str
    lose_pitcher: str
    save_pitcher: str

    @property
    def is_cancelled(self) -> bool:
        return self.state == GAME_STATE_CANCELLED

    @property
    def is_ended(self) -> bool:
        return self.state == GAME_STATE_ENDED


def _game_center_post(url: str, date_str: str) -> dict:
    resp = requests.post(
        url,
        data={"leId": "1", "srId": GAME_SERIES_IDS, "date": date_str},
        headers={
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": GAME_CENTER_REFERER,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_previous_game_date(target: date) -> date | None:
    """target 기준으로 경기가 있었던 직전 날짜를 가져온다 (휴식일은 자동으로 건너뛴다)."""
    data = _game_center_post(GAME_DATE_URL, target.strftime("%Y%m%d"))
    before = data.get("BEFORE_G_DT")
    if not before:
        return None
    return datetime.strptime(before, "%Y%m%d").date()


def fetch_games_for_date(target: date) -> list:
    """해당 날짜에 열린(예정이었던) 모든 경기 결과를 가져온다."""
    data = _game_center_post(GAME_LIST_URL, target.strftime("%Y%m%d"))
    games = []
    for g in data.get("game", []):
        games.append(
            GameResult(
                game_id=g.get("G_ID", ""),
                away_team=g.get("AWAY_NM", ""),
                home_team=g.get("HOME_NM", ""),
                away_score=int(g.get("T_SCORE_CN") or 0),
                home_score=int(g.get("B_SCORE_CN") or 0),
                state=g.get("GAME_STATE_SC", ""),
                cancel_reason=g.get("CANCEL_SC_NM", ""),
                stadium=g.get("S_NM", ""),
                win_pitcher=(g.get("W_PIT_P_NM") or "").strip(),
                lose_pitcher=(g.get("L_PIT_P_NM") or "").strip(),
                save_pitcher=(g.get("SV_PIT_P_NM") or "").strip(),
            )
        )
    return games


def fetch_latest_game_results(today: date | None = None):
    """오늘 기준 가장 최근에 경기가 열렸던 날짜와 그 날의 전체 경기 결과를 가져온다.

    KBO 공식 사이트가 계산하는 "직전 경기일"(BEFORE_G_DT)을 그대로 사용하므로,
    월요일처럼 경기가 없는 날은 자동으로 건너뛴다.
    """
    today = today or date.today()
    prev_date = fetch_previous_game_date(today)
    if prev_date is None:
        return None, []
    return prev_date, fetch_games_for_date(prev_date)


@dataclass
class RosterPeriod:
    team: str
    start: str
    end: str
    days: str  # "10 (부상자명단)"처럼 사유가 함께 표기되는 경우가 있어 텍스트로 보관


def _parse_roster_periods(html: str) -> dict:
    """선수 상세 페이지의 'KBO 리그 엔트리 등록/말소 일수' 표를 파싱한다."""
    soup = BeautifulSoup(html, "lxml")
    panel = soup.select_one("#cphContents_cphContents_cphContents_pnlRoster")
    result = {"registered": [], "registered_total": None, "cancelled": [], "cancelled_total": None}
    if not panel:
        return result

    def _parse_table(table):
        rows = []
        for tr in table.select("tbody tr"):
            cells = tr.select("th, td")
            if len(cells) != 4:
                continue
            team, start, end, days = [c.get_text(strip=True) for c in cells]
            rows.append(RosterPeriod(team=team, start=start, end=end, days=days))
        total_cell = table.select_one("tfoot td:last-child")
        total = total_cell.get_text(strip=True) if total_cell else None
        return rows, total

    tables = panel.select("div.tbl-type02 table.tbl")
    if len(tables) > 0:
        result["registered"], result["registered_total"] = _parse_table(tables[0])
    if len(tables) > 1:
        result["cancelled"], result["cancelled_total"] = _parse_table(tables[1])
    return result


def fetch_roster_periods(player_id: str, position: str) -> dict:
    """선수의 이번 시즌 KBO 리그 엔트리 등록/말소 기간(일수) 이력을 가져온다."""
    url_template = PITCHER_DETAIL_URL["1군"] if position == "투" else HITTER_DETAIL_URL["1군"]
    resp = requests.get(url_template.format(player_id=player_id), headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return _parse_roster_periods(resp.text)
