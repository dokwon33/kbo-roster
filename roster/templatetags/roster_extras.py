from datetime import date

from django import template

from roster.models import RosterEvent

register = template.Library()

_TEAM_LOGO_CODE = {
    "삼성": "SS",
    "KT": "KT",
    "kt": "KT",
    "LG": "LG",
    "KIA": "HT",
    "두산": "OB",
    "한화": "HH",
    "NC": "NC",
    "롯데": "LT",
    "SSG": "SK",
    "키움": "WO",
    "고양": "WO",  # 2군(퓨처스) 키움 히어로즈 2군 연고지 표기
    "상무": "SM",  # 2군(퓨처스) 상무
    "울산": "UL",  # 2군(퓨처스) 롯데 2군 연고지 표기
}
_TEAM_LOGO_URL_TEMPLATE = (
    "//6ptotvmi5753.edge.naverncp.com/KBO_IMAGE/emblem/regular/{year}/emblemBF_{code}.png"
)

_BADGE_CLASS = {
    RosterEvent.ACTIVE_1GUN: "badge-active",
    RosterEvent.OPTIONED_2GUN: "badge-optioned",
    RosterEvent.INJURED: "badge-injured",
}


@register.filter
def badge_class(event):
    if not event:
        return "badge-other"
    return _BADGE_CLASS.get(event.event_type, "badge-other")


@register.filter
def status_label(event):
    return event.get_event_type_display() if event else "미상"


@register.filter
def team_logo_url(team_name):
    """구단명을 KBO 공식 엠블럼 이미지 URL로 변환한다. 매칭되는 구단이 없으면 빈 문자열(상무 등)."""
    code = _TEAM_LOGO_CODE.get(team_name)
    if not code:
        return ""
    return _TEAM_LOGO_URL_TEMPLATE.format(year=date.today().year, code=code)


@register.filter
def streak_class(streak):
    if not streak:
        return ""
    if streak.endswith("승"):
        return "streak-win"
    if streak.endswith("패"):
        return "streak-loss"
    return ""
