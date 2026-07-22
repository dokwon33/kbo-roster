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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; kbo-roster-tracker/1.0)"}

DATE_LABEL_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


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
