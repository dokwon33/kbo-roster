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

    return PlayerProfile(
        kbo_player_id=player_id,
        birth_date=birth_date,
        team=row_team,
        position=position,
        photo_url=photo_url,
    )
