import re
import secrets

from django.conf import settings
from django.core.cache import cache

from . import stats

_EXCLUDED_PREFIXES = ("/static/", "/sync/", "/stats/")
_VISITOR_COOKIE = "vid"
_BOT_UA_RE = re.compile(
    r"bot|spider|crawl|slurp|bingpreview|facebookexternalhit|whatsapp|"
    r"telegrambot|discordbot|headless|python-requests|curl|wget",
    re.IGNORECASE,
)


def _is_bot(request):
    ua = request.META.get("HTTP_USER_AGENT", "")
    return not ua or bool(_BOT_UA_RE.search(ua))


class PageViewMiddleware:
    """실제 페이지 조회만 방문수로 집계한다 (정적 파일, /sync/, /stats/ 자체는 제외).

    페이지뷰(page_views)는 요청마다, 순방문자(unique_visitors)는 방문자 쿠키
    기준 하루 1회만 집계한다. 봇으로 보이는 User-Agent는 아예 집계에서 뺀다.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.method == "GET"
            and response.status_code == 200
            and not request.path.startswith(_EXCLUDED_PREFIXES)
            and not _is_bot(request)
        ):
            vid = request.COOKIES.get(_VISITOR_COOKIE)
            if not vid:
                vid = secrets.token_hex(16)
                response.set_cookie(
                    _VISITOR_COOKIE,
                    vid,
                    max_age=60 * 60 * 24 * 365,
                    httponly=True,
                    samesite="Lax",
                    secure=not settings.DEBUG,
                )

            cache_key = f"pv:{vid}"
            is_unique_visitor = cache.get(cache_key) is None
            if is_unique_visitor:
                cache.set(cache_key, True, timeout=60 * 60 * 24)

            stats.record_page_view(is_unique_visitor=is_unique_visitor)
        return response
