"""네이버 뉴스 검색 API를 통한 선수 관련 기사 수집."""
import re
from dataclasses import dataclass
from html import unescape

import requests
from django.conf import settings

NAVER_NEWS_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

_TAG_RE = re.compile(r"<[^>]+>")
_HANJA_RE = re.compile(r"[一-鿿]")


def _clean_text(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


@dataclass
class NewsArticle:
    title: str
    description: str
    link: str
    pub_date: str


def fetch_player_news(player_name: str, team_name: str = None, display: int = 10) -> list:
    """선수 이름(+소속팀)으로 네이버 뉴스를 검색해 최신순으로 반환한다.

    검색어에 소속팀을 함께 넣어 동명이인·해외리그 동명 선수 기사가 덜 걸리도록 하고,
    한자가 섞인 기사(주로 번역·해외 매체발 저품질 기사)는 사전에 제외한다.
    """
    if not settings.NAVER_CLIENT_ID or not settings.NAVER_CLIENT_SECRET:
        return []

    query = f"{player_name} {team_name}" if team_name else player_name
    resp = requests.get(
        NAVER_NEWS_SEARCH_URL,
        params={"query": query, "display": display * 2, "sort": "date"},
        headers={
            "X-Naver-Client-Id": settings.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    articles = [
        NewsArticle(
            title=_clean_text(item.get("title", "")),
            description=_clean_text(item.get("description", "")),
            link=item.get("originallink") or item.get("link", ""),
            pub_date=item.get("pubDate", ""),
        )
        for item in items
    ]
    articles = [a for a in articles if not _HANJA_RE.search(a.title + a.description)]
    return articles[:display]
