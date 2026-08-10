from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from roster.models import Player, RosterEvent, Team
from roster.push import send_push_to_team
from roster.scraping import fetch_current, fetch_for_date, resolve_player_profile


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
        for row, event_type in (
            *((r, RosterEvent.ACTIVE_1GUN) for r in result.registered),
            *((r, RosterEvent.OPTIONED_2GUN) for r in result.cancelled),
        ):
            team, _ = Team.objects.get_or_create(name=row.team)
            player, player_created = Player.objects.get_or_create(
                name=row.name,
                defaults={"team": team, "position": row.position},
            )

            if player_created:
                self._enrich_profile(player, row.team)

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
                self._notify_team(team, player, event_type)

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

    def _notify_team(self, team, player, event_type):
        if event_type == RosterEvent.ACTIVE_1GUN:
            emoji, title_label, body_verb = "⬆️", "1군 콜업!", "1군에 등록됐어요"
        else:
            emoji, title_label, body_verb = "⬇️", "2군행", "2군으로 말소됐어요"

        position = player.get_position_display() or "-"
        back_number = f"#{player.back_number} · " if player.back_number else ""

        try:
            send_push_to_team(
                team,
                title=f"{emoji} {player.name} {title_label}",
                body=f"{team.name} · {back_number}{position} · {body_verb}",
                url=f"/players/{player.id}/",
            )
        except Exception as exc:  # 푸시 발송 실패가 동기화 자체를 막으면 안 된다
            self.stderr.write(f"{team.name} 푸시 발송 실패: {exc}")

    def _enrich_profile(self, player, team_name):
        """신규 선수에 한해 KBO 선수 조회 페이지에서 고유 코드/생년월일/사진 URL을 채운다."""
        try:
            profile = resolve_player_profile(player.name, team=team_name)
        except Exception as exc:  # 네트워크 오류 등으로 프로필 조회에 실패해도 동기화 자체는 계속 진행
            self.stderr.write(f"{player.name} 프로필 조회 실패: {exc}")
            return
        if not profile:
            return
        player.kbo_player_id = profile.kbo_player_id
        player.birth_date = profile.birth_date
        player.photo_url = profile.photo_url
        if profile.back_number:
            player.back_number = profile.back_number
        player.save(update_fields=["kbo_player_id", "birth_date", "photo_url", "back_number"])
