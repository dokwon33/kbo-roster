from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from roster.models import Player, RosterEvent, Team
from roster.scraping import fetch_current, fetch_for_date


class Command(BaseCommand):
    help = "KBO 공식 사이트에서 1군 등록/말소 현황을 가져와 DB에 반영합니다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="특정 날짜(YYYYMMDD)의 현황을 가져옵니다. 생략하면 사이트의 현재 기준일을 사용합니다.",
        )

    def handle(self, *args, **options):
        if options.get("date"):
            try:
                target = datetime.strptime(options["date"], "%Y%m%d").date()
            except ValueError as exc:
                raise CommandError("--date 형식은 YYYYMMDD 여야 합니다.") from exc
            result = fetch_for_date(target)
        else:
            result = fetch_current()

        created = 0
        with transaction.atomic():
            for row, event_type in (
                *((r, RosterEvent.ACTIVE_1GUN) for r in result.registered),
                *((r, RosterEvent.OPTIONED_2GUN) for r in result.cancelled),
            ):
                team, _ = Team.objects.get_or_create(name=row.team)
                player, _ = Player.objects.get_or_create(
                    name=row.name,
                    defaults={"team": team, "position": row.position},
                )

                last_event = player.current_status
                is_new = (
                    last_event is None
                    or last_event.event_type != event_type
                    or last_event.team_id != team.id
                )
                if is_new:
                    RosterEvent.objects.update_or_create(
                        player=player,
                        event_date=result.as_of,
                        event_type=event_type,
                        defaults={"team": team, "source": RosterEvent.SOURCE_SCRAPER},
                    )
                    created += 1

                if player.team_id != team.id or player.position != row.position:
                    player.team = team
                    player.position = row.position
                    player.save(update_fields=["team", "position"])

        self.stdout.write(
            self.style.SUCCESS(
                f"{result.as_of} 기준 등록 {len(result.registered)}명 / "
                f"말소 {len(result.cancelled)}명 처리, 신규 이벤트 {created}건 생성"
            )
        )
