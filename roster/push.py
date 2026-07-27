"""마이팀 로스터 변동 Web Push 알림 발송."""
import json
import logging

from django.conf import settings
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from .models import PushSubscription

logger = logging.getLogger(__name__)


def _vapid():
    if not settings.VAPID_PRIVATE_KEY_PEM:
        return None
    return Vapid.from_pem(settings.VAPID_PRIVATE_KEY_PEM.encode())


def send_push_to_team(team, title, body, url="/"):
    """해당 팀을 구독 중인 모든 브라우저에 알림을 보낸다. 만료된 구독은 삭제한다."""
    if not settings.VAPID_PRIVATE_KEY_PEM or not settings.VAPID_CLAIM_EMAIL:
        return

    vapid = _vapid()
    payload = json.dumps({"title": title, "body": body, "url": url})

    for sub in PushSubscription.objects.filter(team=team):
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
            )
        except WebPushException as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in (404, 410):
                sub.delete()
            else:
                logger.warning("푸시 발송 실패 (%s): %s", sub.endpoint[:60], exc)
