"""네이버 뉴스 검색 API를 통한 선수 관련 기사 수집."""
import re
from dataclasses import dataclass
from html import unescape

import requests
from django.conf import settings

NAVER_NEWS_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(text: str) -> str:
    return unescape(_TAG_RE.sub("", text)).strip()


@dataclass
class NewsArticle:
    title: str
    description: str
    link: str
    pub_date: str


def fetch_player_news(query: str, display: int = 10) -> list:
    """선수 이름으로 네이버 뉴스를 검색해 최신순으로 반환한다."""
    if not settings.NAVER_CLIENT_ID or not settings.NAVER_CLIENT_SECRET:
        return []

    resp = requests.get(
        NAVER_NEWS_SEARCH_URL,
        params={"query": query, "display": display, "sort": "date"},
        headers={
            "X-Naver-Client-Id": settings.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": settings.NAVER_CLIENT_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [
        NewsArticle(
            title=_clean_text(item.get("title", "")),
            description=_clean_text(item.get("description", "")),
            link=item.get("originallink") or item.get("link", ""),
            pub_date=item.get("pubDate", ""),
        )
        for item in items
    ]
