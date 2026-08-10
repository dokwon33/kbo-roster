"""프롬프트 golden set(fixtures/prompt_eval/) 채점 스크립트.

fixtures/prompt_eval/*.json 에 얼려둔 실제 기사 + 프롬프트 버전별 요약을 대상으로
- 룰 기반 자동 체크 (한자 혼입, 해외리그 키워드 혼입, 소속팀 언급 여부)
- (선택) LLM-as-judge 채점 (사실 기반 여부, 인물 식별 정확성, 본인 관련성, 자연스러움)
- 토큰 사용량 추정 (tiktoken 로컬 계산, API 호출 없이 버전 간 비교 가능)
을 실행하고 카테고리별 집계 표를 출력한다.

토큰 계산은 실제 GROQ 모델(gpt-oss-120b)의 정확한 토크나이저가 아니라 cl100k_base로 추정한
근사치다. 절대값(과금 정확도)이 아니라 "프롬프트를 이렇게 바꿨을 때 토큰이 늘었는지 줄었는지"를
버전 간 상대 비교하는 용도로만 쓴다.

사용법:
    python manage.py score_prompt_eval                        # v0(baseline) 룰 기반 채점만
    python manage.py score_prompt_eval --prompt-version v1     # results/v1.json의 요약을 채점
    python manage.py score_prompt_eval --judge                 # LLM-as-judge 채점도 함께 실행 (느림, API 비용 발생)
"""
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import requests
import tiktoken
from django.conf import settings
from django.core.management.base import BaseCommand

FIXTURE_DIR = Path("fixtures/prompt_eval")
RESULTS_DIR = FIXTURE_DIR / "results"

_HANJA_RE = re.compile(r"[一-鿿]")
_FOREIGN_LEAGUE_KEYWORDS = (
    "메이저리그", "MLB", "빅리그", "일본프로야구", "NPB",
    "다저스", "양키스", "마이애미", "자이언츠(미국)",
)

_TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")

# 최초 배포 프롬프트(v0) 스냅샷. llm.py는 이후 버전으로 계속 바뀌므로, v0 기준
# 토큰 계산을 위해 여기 별도로 얼려둔다 (roster/llm.py의 2026-08 초기 버전과 동일).
_V0_SYSTEM_PROMPT = (
    "당신은 KBO 야구 뉴스를 정리해주는 어시스턴트입니다. "
    "주어진 기사 목록에 있는 내용만 근거로 답하고, 기사에 없는 내용은 추측하지 마세요."
    "동명이인이 있을 수 있는데 이때 우선 야구 선수인지 확인해야합니다."
    "만약 야구와 관련없는 내용이라면 해당 기사는 무시하세요."
    "답변은 반드시 한글과 아라비아 숫자, 기본 문장부호만 사용해 한국어로 작성하고, "
    "한자(漢字)나 다른 언어 문자는 절대 섞지 마세요."
)
_V0_USER_PROMPT_TEMPLATE = """선수 "{player_name}"에 대해 검색된 최근 기사 목록입니다. 이 기사들의 내용을 바탕으로
이 선수에게 최근 어떤 일이 있었는지 3~5문장으로 자연스럽게 설명해주세요.

기사 목록:
{articles_text}

설명:"""


def _count_tokens(text):
    if not text:
        return 0
    return len(_TOKEN_ENCODING.encode(text))


def _token_usage(system_prompt, user_prompt, summary):
    return {
        "prompt_tokens": _count_tokens(system_prompt) + _count_tokens(user_prompt),
        "completion_tokens": _count_tokens(summary),
    }

_JUDGE_SYSTEM_PROMPT = (
    "당신은 한국어 야구 뉴스 요약의 품질을 채점하는 평가자입니다. "
    "주어진 원문 기사 목록과 생성된 요약을 비교해 아래 JSON 형식으로만 답하세요. "
    '{"faithfulness": 1-5 정수, "identity_correct": true/false, '
    '"relevance": 1-5 정수, "notes": "한 문장 이내 코멘트"} '
    "faithfulness는 원문에 없는 내용을 지어내지 않았는지, identity_correct는 요청한 선수 "
    "본인에 대한 내용인지(다른 동명이인이나 해외리그 동명 선수와 섞이지 않았는지), "
    "relevance는 팀 전체 뉴스가 아니라 이 선수 본인 얘기에 집중했는지를 뜻합니다. "
    "중요: 원문 기사 전부가 요청한 선수와 무관한 동명이인/다른 인물에 대한 내용이라면, "
    "요약이 '관련 기사를 찾을 수 없다'는 취지로 짧게 거절한 것이 정답입니다. "
    "이 경우 faithfulness=5, identity_correct=true, relevance=5로 채점하세요 — "
    "설명 없이 짧게 거절했다는 이유만으로 감점하지 마세요."
)


def _rule_check(summary, team):
    if not summary:
        return {"hanja_found": None, "foreign_league_mentioned": None, "own_team_mentioned": None}
    hanja_found = bool(_HANJA_RE.search(summary))
    foreign_league_mentioned = any(kw in summary for kw in _FOREIGN_LEAGUE_KEYWORDS)
    own_team_mentioned = bool(team) and team in summary
    return {
        "hanja_found": hanja_found,
        "foreign_league_mentioned": foreign_league_mentioned,
        "own_team_mentioned": own_team_mentioned,
    }


def _llm_judge(player_name, team, articles, summary, max_retries=6):
    if not summary or not settings.GROQ_API_KEY:
        return None

    articles_text = "\n".join(f"- [{a['pub_date']}] {a['title']}: {a['description']}" for a in articles)
    user_prompt = (
        f'선수 이름: "{player_name}" (소속팀: {team or "미상"})\n\n'
        f"원문 기사 목록:\n{articles_text}\n\n"
        f"생성된 요약:\n{summary}\n\n"
        "위 형식의 JSON으로만 채점해주세요."
    )

    resp = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                json={
                    "model": settings.GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=30,
            )
            print(f"    [{player_name}] 시도 {attempt + 1}: status={resp.status_code}", flush=True)
            if resp.status_code == 429:
                print(f"    [{player_name}] 429 응답 본문: {resp.text[:300]}", flush=True)
                time.sleep(30)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            print(f"    [{player_name}] 예외: {exc}", flush=True)
            if attempt == max_retries - 1:
                return {"error": str(exc)}
            time.sleep(30)
    else:
        return {"error": "429 재시도 초과"}

    content = resp.json()["choices"][0]["message"]["content"].strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return {"error": f"JSON 파싱 실패: {content[:200]}"}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return {"error": f"JSON 파싱 실패: {content[:200]}"}


class Command(BaseCommand):
    help = "fixtures/prompt_eval/의 golden set을 프롬프트 버전별로 채점한다."

    def add_arguments(self, parser):
        parser.add_argument("--prompt-version", default="v0", help="채점할 프롬프트 버전 (기본: v0 baseline)")
        parser.add_argument("--judge", action="store_true", help="LLM-as-judge 채점도 함께 실행")

    def handle(self, *args, **options):
        version = options["prompt_version"]
        run_judge = options["judge"]

        fixture_files = sorted(f for f in FIXTURE_DIR.glob("*.json") if f.name != "manifest.json")
        if not fixture_files:
            self.stderr.write("fixtures/prompt_eval/에 fixture가 없습니다.")
            return

        if version == "v0":
            summaries = None  # 각 fixture의 baseline_summary.output을 그대로 사용
            system_prompt = _V0_SYSTEM_PROMPT
            user_prompt_template = _V0_USER_PROMPT_TEMPLATE
            rendered_user_prompts = None  # v0는 fixture articles로부터 그때그때 렌더링
        else:
            results_path = RESULTS_DIR / f"{version}.json"
            if not results_path.exists():
                self.stderr.write(f"{results_path}가 없습니다. 먼저 해당 버전 요약을 생성해두세요.")
                return
            results_data = json.loads(results_path.read_text(encoding="utf-8"))
            if "summaries" in results_data:
                # 신 포맷: 실제 생성에 쓴 system/user 프롬프트 원문까지 같이 저장된 결과
                summaries = results_data["summaries"]
                system_prompt = results_data.get("system_prompt", "")
                rendered_user_prompts = results_data.get("rendered_user_prompts", {})
            else:
                # 구 포맷: {선수명: 요약} 뿐이라 토큰 계산은 건너뜀
                summaries = results_data
                system_prompt = None
                rendered_user_prompts = None
            user_prompt_template = None

        rows = []
        for path in fixture_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data["player_name"]
            category = data["category"]
            team = data.get("team")
            summary = data["baseline_summary"]["output"] if summaries is None else summaries.get(name)

            row = {
                "player_name": name,
                "category": category,
                "summary": summary,
                "rule": _rule_check(summary, team),
            }

            if system_prompt is not None:
                if rendered_user_prompts is not None:
                    user_prompt = rendered_user_prompts.get(name, "")
                else:
                    articles_text = "\n".join(
                        f"- [{a['pub_date']}] {a['title']}: {a['description']}" for a in data["articles"]
                    )
                    user_prompt = user_prompt_template.format(player_name=name, articles_text=articles_text)
                row["tokens"] = _token_usage(system_prompt, user_prompt, summary)

            if run_judge:
                self.stdout.write(f"채점 중 (judge): {name}")
                row["judge"] = _llm_judge(name, team, data["articles"], summary)
                time.sleep(30)  # GROQ TPM 레이트리밋 방지

            rows.append(row)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR / f"{version}_scores.json"
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

        self._print_report(version, rows, run_judge)
        self.stdout.write(self.style.SUCCESS(f"\n채점 결과 저장: {out_path}"))

    def _print_report(self, version, rows, run_judge):
        self.stdout.write(f"\n=== 프롬프트 {version} 채점 결과 ===\n")

        by_category = defaultdict(list)
        for row in rows:
            by_category[row["category"]].append(row)

        for category, items in by_category.items():
            self.stdout.write(f"\n[{category}] ({len(items)}건)")
            for row in items:
                rule = row["rule"]
                flags = []
                if rule["hanja_found"]:
                    flags.append("한자혼입")
                if rule["foreign_league_mentioned"]:
                    flags.append("해외리그혼입")
                if rule["own_team_mentioned"] is False:
                    flags.append("소속팀미언급")
                flag_str = ", ".join(flags) if flags else "이상없음"

                judge_str = ""
                if run_judge and row.get("judge") and "error" not in row["judge"]:
                    j = row["judge"]
                    judge_str = (
                        f" | judge: faithfulness={j.get('faithfulness')} "
                        f"identity_correct={j.get('identity_correct')} relevance={j.get('relevance')}"
                    )

                token_str = ""
                if row.get("tokens"):
                    t = row["tokens"]
                    token_str = f" | 토큰: 입력~{t['prompt_tokens']} 출력~{t['completion_tokens']}"

                self.stdout.write(f"  {row['player_name']:<6} 룰체크: {flag_str}{judge_str}{token_str}")

        token_rows = [row for row in rows if row.get("tokens")]
        if token_rows:
            total_prompt = sum(r["tokens"]["prompt_tokens"] for r in token_rows)
            total_completion = sum(r["tokens"]["completion_tokens"] for r in token_rows)
            n = len(token_rows)
            self.stdout.write(
                f"\n[토큰 사용량 추정 - {n}건, cl100k_base 근사치]\n"
                f"  입력 합계 {total_prompt} (평균 {total_prompt / n:.0f}) / "
                f"출력 합계 {total_completion} (평균 {total_completion / n:.0f}) / "
                f"합계 {total_prompt + total_completion}"
            )
