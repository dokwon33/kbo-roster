from django import template

from roster.models import RosterEvent

register = template.Library()

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
