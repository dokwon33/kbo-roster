"""현재 roster/llm.py에 있는 프롬프트로 golden set(fixtures/prompt_eval/) 요약을 다시
생성해 fixtures/prompt_eval/results/<version>.json에 저장한다.

기사 목록은 fixture에 얼려둔 걸 그대로 재사용한다 (재크롤링 X) — 그래야 입력이 고정된 채로
프롬프트 버전 차이만 관찰할 수 있다. score_prompt_eval이 토큰까지 계산할 수 있도록, 실제
생성에 쓰인 system prompt와 렌더링된 user prompt 원문도 같이 저장한다.

사용법:
    python manage.py generate_prompt_eval --prompt-version v1
"""
import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from roster import llm
from roster.news import NewsArticle

FIXTURE_DIR = Path("fixtures/prompt_eval")
RESULTS_DIR = FIXTURE_DIR / "results"


class Command(BaseCommand):
    help = "현재 llm.py 프롬프트로 golden set 요약을 재생성해 버전별 결과 파일로 저장한다."

    def add_arguments(self, parser):
        parser.add_argument("--prompt-version", required=True, help="결과를 저장할 버전 태그 (예: v1)")
        parser.add_argument("--sleep", type=float, default=16, help="호출 간 대기 초 (GROQ TPM 레이트리밋 방지)")

    def handle(self, *args, **options):
        version = options["prompt_version"]
        if version == "v0":
            raise CommandError(
                "v0는 fixtures/prompt_eval/*.json의 baseline_summary를 그대로 쓰므로 재생성하지 않습니다."
            )
        sleep_s = options["sleep"]

        fixture_files = sorted(f for f in FIXTURE_DIR.glob("*.json") if f.name != "manifest.json")
        if not fixture_files:
            self.stderr.write("fixtures/prompt_eval/에 fixture가 없습니다.")
            return

        summaries = {}
        rendered_user_prompts = {}

        for path in fixture_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data["player_name"]
            team = data.get("team")
            articles = [NewsArticle(**a) for a in data["articles"]]

            self.stdout.write(f"생성 중: {name}")
            summary = None
            for attempt in range(3):
                summary = llm.summarize_player_news(name, team, articles)
                if summary:
                    break
                self.stdout.write(f"  실패 (시도 {attempt + 1}/3), 대기 후 재시도")
                time.sleep(sleep_s)

            summaries[name] = summary
            articles_text = "\n".join(f"- [{a.pub_date}] {a.title}: {a.description}" for a in articles)
            rendered_user_prompts[name] = llm._USER_PROMPT_TEMPLATE.format(
                player_name=name, team_name=team or "미상", articles_text=articles_text
            )
            time.sleep(sleep_s)

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR / f"{version}.json"
        out_path.write_text(
            json.dumps(
                {
                    "system_prompt": llm._SYSTEM_PROMPT,
                    "rendered_user_prompts": rendered_user_prompts,
                    "summaries": summaries,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        missing = [name for name, s in summaries.items() if not s]
        if missing:
            self.stderr.write(f"요약 생성 실패한 케이스: {', '.join(missing)}")
        self.stdout.write(self.style.SUCCESS(f"저장 완료: {out_path}"))
