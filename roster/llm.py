"""Groq 호스팅 API(오픈소스 모델)를 통한 뉴스 요약/설명 생성."""
import re

import requests
from django.conf import settings

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# 다국어 모델이 한국어 생성 중 한자를 섞어 쓰는 경우가 있어, 프롬프트 지시와 별개로
# CJK 통합 한자 영역(한글이 아닌 한자)을 후처리로 한 번 더 제거한다.
_HANJA_RE = re.compile(r"[一-鿿]")

_SYSTEM_PROMPT = (
    "당신은 KBO 야구 뉴스를 정리해주는 어시스턴트입니다. "
    "주어진 기사 목록에 있는 내용만 근거로 답하고, 기사에 없는 내용은 추측하지 마세요."
    "동명이인이 있을 수 있는데 이때 우선 야구 선수인지 확인해야합니다."
    "만약 야구와 관련없는 내용이라면 해당 기사는 무시하세요."
    "답변은 반드시 한글과 아라비아 숫자, 기본 문장부호만 사용해 한국어로 작성하고, "
    "한자(漢字)나 다른 언어 문자는 절대 섞지 마세요."
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
    content = choices[0].get("message", {}).get("content", "").strip()
    content = _HANJA_RE.sub("", content)
    return content or None
