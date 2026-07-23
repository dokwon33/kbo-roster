"""로컬 Ollama를 통한 뉴스 요약/설명 생성."""
import requests
from django.conf import settings

_PROMPT_TEMPLATE = """당신은 KBO 야구 뉴스를 정리해주는 어시스턴트입니다.
아래는 선수 "{player_name}"에 대해 검색된 최근 기사 목록입니다. 이 기사들의 내용을 바탕으로
이 선수에게 최근 어떤 일이 있었는지 3~5문장으로 자연스럽게 설명해주세요.
기사에 없는 내용은 추측하지 말고, 기사 내용에 없으면 언급하지 마세요.

기사 목록:
{articles_text}

설명:"""


def summarize_player_news(player_name: str, articles: list) -> str | None:
    if not articles:
        return None

    articles_text = "\n".join(
        f"- [{a.pub_date}] {a.title}: {a.description}" for a in articles
    )
    prompt = _PROMPT_TEMPLATE.format(player_name=player_name, articles_text=articles_text)

    try:
        resp = requests.post(
            f"{settings.OLLAMA_URL}/api/generate",
            json={"model": settings.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    return resp.json().get("response", "").strip() or None
