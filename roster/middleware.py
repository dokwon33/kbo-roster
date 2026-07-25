from . import stats

_EXCLUDED_PREFIXES = ("/static/", "/sync/", "/stats/")


class PageViewMiddleware:
    """실제 페이지 조회만 방문수로 집계한다 (정적 파일, /sync/, /stats/ 자체는 제외)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.method == "GET"
            and response.status_code == 200
            and not request.path.startswith(_EXCLUDED_PREFIXES)
        ):
            stats.record_page_view()
        return response
