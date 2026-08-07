"""방문자수/LLM 호출 수 집계용 헬퍼. 미들웨어와 llm.py에서 호출한다."""
from django.db.models import F
from django.utils import timezone

from .models import DailyStat


def _increment(field):
    today = timezone.localdate()
    DailyStat.objects.get_or_create(date=today)
    DailyStat.objects.filter(date=today).update(**{field: F(field) + 1})


def record_page_view(is_unique_visitor=False):
    _increment("page_views")
    if is_unique_visitor:
        _increment("unique_visitors")


def record_llm_call():
    _increment("llm_calls")
