"""Groq 호스팅 API(오픈소스 모델)를 통한 뉴스 요약/설명 생성."""
import re

import requests
from django.conf import settings

from . import stats

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# 프롬프트를 바꿀 때마다 올려서 NewsSummaryLog에 함께 남긴다 (버전별 비교용).
PROMPT_VERSION = "v2"

# 다국어 모델이 한국어 생성 중 한자를 섞어 쓰는 경우가 있어, 프롬프트 지시와 별개로
# CJK 통합 한자 영역(한글이 아닌 한자)을 후처리로 한 번 더 제거한다.
_HANJA_RE = re.compile(r"[一-鿿]")

_SYSTEM_PROMPT = (
    "당신은 KBO 야구 뉴스를 정리해주는 어시스턴트입니다. "
    "주어진 기사 목록에 있는 내용만 근거로 답하고, 기사에 없는 내용은 추측하지 마세요. "
    "동명이인이 있을 수 있으니, 각 기사가 정말 이 선수 본인에 대한 내용인지 다음을 모두 확인하세요: "
    "1) 야구 선수인가, 2) KBO 소속인가(메이저리그·일본프로야구 등 해외리그 선수는 동명이인으로 간주), "
    "3) 알려준 소속팀과 일치하는가. "
    "위 조건에 맞는 기사가 하나도 없으면 억지로 요약하지 말고 "
    "\"최근 관련 기사를 찾을 수 없습니다.\"라고만 답하세요. "
    "각 기사 앞의 [날짜]는 그 기사가 '발행된' 날짜일 뿐, 본문에 언급된 사건이 그 날짜에 "
    "일어났다는 뜻이 아닙니다. 기사가 과거 배경이나 다른 소식을 다루다가 이 선수를 짧게 "
    "언급하는 경우, 그 언급된 사건의 날짜를 기사 발행일로 단정하지 마세요. 사건이 언제 "
    "일어났는지는 본문에 명시된 표현(예: '8월 4일', '어제', '이번 시즌')이 있을 때만 그 날짜를 "
    "쓰고, 불확실하면 날짜를 아예 언급하지 마세요. "
    "답변은 반드시 한글과 아라비아 숫자, 기본 문장부호만 사용해 한국어로 작성하고, "
    "한자(漢字)나 다른 언어 문자는 절대 섞지 마세요."
)

_USER_PROMPT_TEMPLATE = """선수 "{player_name}"(소속팀: {team_name})에 대해 검색된 최근 기사 목록입니다. 이 기사들의 내용을 바탕으로
이 선수에게 최근 어떤 일이 있었는지 3~5문장으로 자연스럽게 설명해주세요.

기사 목록:
{articles_text}

설명:"""


def _call_groq(system_prompt: str, user_prompt: str) -> str | None:
    stats.record_llm_call()
    try:
        resp = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
            json={
                "model": settings.GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
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


def summarize_player_news(player_name: str, team_name: str, articles: list) -> str | None:
    if not articles or not settings.GROQ_API_KEY:
        return None

    articles_text = "\n".join(
        f"- [{a.pub_date}] {a.title}: {a.description}" for a in articles
    )
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        player_name=player_name, team_name=team_name or "미상", articles_text=articles_text
    )
    return _call_groq(_SYSTEM_PROMPT, user_prompt)


# 방문자가 고를 수 있는 요약 범위 프리셋. "종합 요약"은 기사+시즌 성적을 함께 근거로 쓰고,
# "스탯 위주"는 성적 숫자만 근거로 쓴다 — 둘 다 fixed preset이라 조합이 늘어나지 않는다.
STYLE_PRESETS = {
    "comprehensive": {"label": "종합 요약"},
    "stats": {"label": "스탯 위주"},
}
DEFAULT_STYLE = "comprehensive"

_UPDATE_SYSTEM_PROMPT = (
    "당신은 KBO 야구 선수 소식을 정리해주는 어시스턴트입니다. "
    "주어진 정보(관련 기사, 이번 시즌 성적)에 있는 내용만 근거로 답하고, 주어지지 않은 내용은 "
    "추측하지 마세요. "
    "기사 목록이 함께 주어지면, 동명이인이 있을 수 있으니 각 기사가 정말 이 선수 본인에 대한 "
    "내용인지 다음을 모두 확인하세요: 1) 야구 선수인가, 2) KBO 소속인가(메이저리그·일본프로야구 등 "
    "해외리그 선수는 동명이인으로 간주), 3) 알려준 소속팀과 일치하는가. 조건에 맞는 기사가 하나도 "
    "없으면 그 사실만 언급하고 억지로 요약하지 마세요. 기사 앞의 [날짜]는 그 기사의 발행일일 뿐 "
    "본문에 언급된 사건의 날짜가 아니니, 사건 날짜는 본문에 명시된 표현이 있을 때만 쓰고 불확실하면 "
    "언급하지 마세요. "
    "답변은 반드시 한글과 아라비아 숫자, 기본 문장부호만 사용해 한국어로 작성하고, "
    "한자(漢字)나 다른 언어 문자는 절대 섞지 마세요."
)

_UPDATE_INSTRUCTIONS = {
    "comprehensive": (
        "아래 정보(관련 기사, 이번 시즌 성적)를 종합해, 이 선수에게 최근 어떤 일이 있었고 "
        "성적은 어떤지 3~5문장으로 자연스럽게 설명해주세요."
    ),
    "stats": (
        "아래 이번 시즌 성적만 근거로 이 선수의 최근 성적이 어떤지 3~5문장으로 설명해주세요. "
        "성적 숫자 해석 외의 다른 이야기는 하지 마세요."
    ),
}

_UPDATE_USER_PROMPT_TEMPLATE = """선수 "{player_name}"(소속팀: {team_name})에 대한 정보입니다.

{instruction}

{sections}

설명:"""


def summarize_player_update(
    player_name: str,
    team_name: str,
    articles: list | None = None,
    season_stats: dict | None = None,
    style: str = DEFAULT_STYLE,
) -> str | None:
    """뉴스 기사와 이번 시즌 성적을 근거로 선수 근황을 요약한다.

    style="stats"는 기사 내용을 프롬프트에서 아예 빼, 순수하게 성적 숫자만 근거로 쓰게 한다.
    """
    if style not in STYLE_PRESETS:
        style = DEFAULT_STYLE
    if style == "stats":
        articles = None

    sections = []
    if articles:
        articles_text = "\n".join(f"- [{a.pub_date}] {a.title}: {a.description}" for a in articles)
        sections.append(f"[관련 기사]\n{articles_text}")
    if season_stats:
        stats_text = "\n".join(f"- {key}: {value}" for key, value in season_stats.items())
        sections.append(f"[이번 시즌 성적]\n{stats_text}")

    if not sections or not settings.GROQ_API_KEY:
        return None

    user_prompt = _UPDATE_USER_PROMPT_TEMPLATE.format(
        player_name=player_name,
        team_name=team_name or "미상",
        instruction=_UPDATE_INSTRUCTIONS[style],
        sections="\n\n".join(sections),
    )
    return _call_groq(_UPDATE_SYSTEM_PROMPT, user_prompt)
