"""Groq 호스팅 API(오픈소스 모델)를 통한 뉴스 요약/설명 생성."""
import requests
from django.conf import settings

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_PROMPT = (
    "당신은 KBO 야구 뉴스를 정리해주는 어시스턴트입니다. "
    "주어진 기사 목록에 있는 내용만 근거로 답하고, 기사에 없는 내용은 추측하지 마세요."
)

_USER_PROMPT_TEMPLATE = """선수 "{player_name}"에 대해 검색된 최근 기사 목록입니다. 이 기사들의 내용을 바탕으로
이 선수에게 최근 어떤 일이 있었는지 3~5문장으로 자연스럽게 설명해주세요.

기사 목록:
{articles_text}

설명:"""


def summarize_player_news(player_name: str, articles: list) -> str | None:
    if not articles or not settings.GROQ_API_KEY:
        return None

    articles_text = "\n".join(
        f"- [{a.pub_date}] {a.title}: {a.description}" for a in articles
    )
    user_prompt = _USER_PROMPT_TEMPLATE.format(player_name=player_name, articles_text=articles_text)

    try:
        resp = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            json={
                "model": settings.GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    choices = resp.json().get("choices", [])
    if not choices:
        return None
    return choices[0].get("message", {}).get("content", "").strip() or None
