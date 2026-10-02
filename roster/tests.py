from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from roster import scraping
from roster.models import Player, RosterEvent, Team
from roster.scraping import AttendanceRow
from roster.views import MAIN_PAGE_STALE_DAYS, ROSTER_STALE_DAYS

# 운영 설정은 whitenoise 매니페스트 스토리지라 collectstatic을 먼저 돌려야 {% static %}이 풀린다.
# 테스트에서까지 그걸 요구하지 않도록 기본 스토리지로 바꾼다.
_STATIC_STORAGE = override_settings(
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
)


def _make_player(name, team, event_date, event_type=RosterEvent.ACTIVE_1GUN):
    player = Player.objects.create(name=name, team=team, position="투")
    RosterEvent.objects.create(
        player=player, team=team, event_date=event_date, event_type=event_type
    )
    return player


@_STATIC_STORAGE
class RosterFreezeTests(TestCase):
    """비시즌에 로스터 명단이 통째로 비지 않는지 검증한다.

    컷오프 기준이 '오늘'이면 등록/말소가 멈춘 뒤 일정 기간이 지나는 순간 모든 선수가
    한꺼번에 탈락한다. 기준을 '마지막 변동일'로 잡았기 때문에 그런 일이 없어야 한다.
    """

    def setUp(self):
        self.team = Team.objects.create(name="KT")
        self.today = timezone.now().date()

    def test_in_season_drops_stale_player(self):
        """시즌 중에는 기존 동작 그대로 — 오래 방치된 선수는 명단에서 빠진다."""
        _make_player("최근선수", self.team, self.today)
        _make_player("방치선수", self.team, self.today - timedelta(days=ROSTER_STALE_DAYS + 5))

        resp = self.client.get(reverse("roster:team_detail", args=[self.team.id]))
        names = [p.name for p in resp.context["active_players"]]

        self.assertIn("최근선수", names)
        self.assertNotIn("방치선수", names)

    def test_offseason_keeps_final_roster(self):
        """반년 동안 변동이 없어도 마지막 시즌 명단은 그대로 남아야 한다."""
        last_change = self.today - timedelta(days=180)
        _make_player("한선수", self.team, last_change)
        _make_player("두선수", self.team, last_change - timedelta(days=3))

        resp = self.client.get(reverse("roster:team_detail", args=[self.team.id]))
        names = [p.name for p in resp.context["active_players"]]

        self.assertEqual(sorted(names), ["두선수", "한선수"])
        self.assertTrue(resp.context["roster_is_frozen"])
        self.assertEqual(resp.context["roster_as_of"], last_change)
        self.assertContains(resp, "이후 등록/말소 변동이 없어")

    def test_offseason_keeps_main_page_cards_filled(self):
        """메인 페이지는 컷오프가 더 짧아 먼저 비는 자리 — 여기도 남아 있어야 한다."""
        last_change = self.today - timedelta(days=MAIN_PAGE_STALE_DAYS * 3)
        _make_player("한선수", self.team, last_change)

        resp = self.client.get(reverse("roster:team_list"))
        card = next(c for c in resp.context["cards"] if c["team"].id == self.team.id)

        self.assertEqual([p.name for p in card["active"]], ["한선수"])
        self.assertFalse(card["has_recent"])

    def test_offseason_keeps_recent_events_page_filled(self):
        last_change = self.today - timedelta(days=180)
        _make_player("한선수", self.team, last_change)

        resp = self.client.get(reverse("roster:recent_events"))

        self.assertEqual(len(resp.context["events"]), 1)
        self.assertTrue(resp.context["roster_is_frozen"])

    def test_new_season_unfreezes_and_drops_last_season(self):
        """다음 시즌 첫 변동이 들어오면 기준일이 따라 올라가 지난 시즌 명단은 정리된다."""
        _make_player("작년선수", self.team, self.today - timedelta(days=180))
        _make_player("올해선수", self.team, self.today)

        resp = self.client.get(reverse("roster:team_detail", args=[self.team.id]))
        names = [p.name for p in resp.context["active_players"]]

        self.assertEqual(names, ["올해선수"])
        self.assertFalse(resp.context["roster_is_frozen"])


class RegularSeasonEndDateTests(TestCase):
    """GameCenter 경기일 응답에서 정규시즌 종료일을 뽑아내는 규칙.

    시즌이 끝난 뒤 조회하면 NOW_G_DT가 '다음 시즌 개막일'로 넘어가버리기 때문에,
    응답값을 그대로 쓰면 종료일이 이듬해 3월로 잡힌다. 연도 필터가 그 함정을 막는다.
    """

    def _fetch(self, payload, year=2026):
        with patch.object(scraping, "_game_center_post", return_value=payload) as m:
            result = scraping.fetch_regular_season_end_date(year)
        return result, m

    def test_uses_now_date_while_season_schedule_remains(self):
        result, mock = self._fetch(
            {"BEFORE_G_DT": "20261006", "NOW_G_DT": "20261007", "AFTER_G_DT": ""}
        )
        self.assertEqual(result, date(2026, 10, 7))
        # 정규시즌(srId=0)만 물어야 포스트시즌 날짜가 섞이지 않는다.
        self.assertEqual(mock.call_args.kwargs["series_ids"], scraping.REGULAR_SEASON_SERIES_ID)

    def test_ignores_next_season_opener(self):
        """시즌 종료 후 NOW_G_DT가 이듬해 개막일로 넘어가면 그 값은 버려야 한다."""
        result, _ = self._fetch(
            {"BEFORE_G_DT": "20261007", "NOW_G_DT": "20270328", "AFTER_G_DT": "20270329"}
        )
        self.assertEqual(result, date(2026, 10, 7))

    def test_returns_none_when_schedule_unknown(self):
        result, _ = self._fetch({"BEFORE_G_DT": "", "NOW_G_DT": "", "AFTER_G_DT": ""})
        self.assertIsNone(result)


@_STATIC_STORAGE
class AttendanceRegularSeasonFilterTests(TestCase):
    """매진율 통계에서 포스트시즌 경기를 제외하는지 검증한다."""

    SEASON_END = date(2026, 10, 7)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _rows(self):
        return [
            # 정규시즌 — 잠실 절반 조금 넘게 채운 경기
            AttendanceRow(date(2026, 9, 1), "화", "LG", "KT", "잠실", 12_000),
            AttendanceRow(self.SEASON_END, "수", "LG", "KT", "잠실", 12_000),
            # 포스트시즌 — 매진. 섞이면 평균이 위로 끌려간다.
            AttendanceRow(date(2026, 10, 9), "금", "LG", "KT", "잠실", 23_750),
        ]

    def _get(self, season_end):
        with patch.object(scraping, "fetch_attendance_rows", return_value=self._rows()), \
             patch.object(scraping, "fetch_regular_season_end_date", return_value=season_end):
            return self.client.get(reverse("roster:attendance_stats"))

    def test_postseason_games_excluded(self):
        resp = self._get(self.SEASON_END)
        stat = resp.context["team_stats"][0]

        self.assertEqual(stat["total_games"], 2)
        self.assertAlmostEqual(stat["avg_rate"], 100 * 12_000 / 23_750, places=4)

    def test_season_end_day_itself_is_included(self):
        """경계값 — 정규시즌 마지막 날 경기는 포함되어야 한다."""
        resp = self._get(self.SEASON_END)
        dates_counted = resp.context["team_stats"][0]["total_games"]
        self.assertEqual(dates_counted, 2)

    def test_falls_back_to_all_rows_when_end_date_unknown(self):
        """종료일을 못 받아오면 통계를 비우지 말고 전체를 쓴다."""
        resp = self._get(None)
        self.assertEqual(resp.context["team_stats"][0]["total_games"], 3)
        self.assertIsNone(resp.context["season_end"])


@_STATIC_STORAGE
class FinalStandingsTests(TestCase):
    """정규시즌이 끝난 순위표는 '현재 순위'가 아니라 '확정된 순위'로 보여야 한다.

    퓨처스가 1군보다 먼저 끝나기 때문에, 두 리그의 확정 여부가 서로 다른 시기가 생긴다.
    """

    FUTURES_END = date(2026, 9, 20)
    REGULAR_END = date(2026, 10, 7)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _get(self, today, regular_end=REGULAR_END, futures_end=FUTURES_END):
        rows = [scraping.TeamStandingRow(
            rank="1", team="LG", games="100", wins="60", losses="40", draws="0",
            win_pct="0.600", games_behind="0", recent_10="5승0무5패", streak="1승",
            home_record="30-0-20", away_record="30-0-20", division="북부",
        )]

        def end_date(year, league_id=scraping.LEAGUE_1GUN):
            return futures_end if league_id == scraping.LEAGUE_FUTURES else regular_end

        with patch.object(scraping, "fetch_standings_1gun", return_value=rows), \
             patch.object(scraping, "fetch_standings_2gun", return_value=rows), \
             patch.object(scraping, "fetch_regular_season_end_date", side_effect=end_date), \
             patch("roster.views.date") as mock_date:
            mock_date.today.return_value = today
            return self.client.get(reverse("roster:standings"))

    def test_futures_final_while_first_team_still_playing(self):
        """9월 말 — 퓨처스만 끝난 상태. 2군에만 표시가 붙어야 한다."""
        resp = self._get(today=date(2026, 9, 29))

        self.assertTrue(resp.context["futures_is_final"])
        self.assertFalse(resp.context["regular_is_final"])
        self.assertContains(resp, "9월 20일 퓨처스리그 정규시즌 종료 기준입니다.")
        self.assertNotContains(resp, "이후 순위는 포스트시즌 결과로 가려집니다.")

    def test_both_final_during_postseason(self):
        """포스트시즌 기간 — 1군 순위도 확정된 정규시즌 순위다."""
        resp = self._get(today=date(2026, 10, 20))

        self.assertTrue(resp.context["regular_is_final"])
        self.assertTrue(resp.context["futures_is_final"])
        self.assertContains(resp, "10월 7일 정규시즌 종료 기준입니다.")
        self.assertContains(resp, "이후 순위는 포스트시즌 결과로 가려집니다.")

    def test_nothing_marked_mid_season(self):
        resp = self._get(today=date(2026, 8, 1))

        self.assertFalse(resp.context["regular_is_final"])
        self.assertFalse(resp.context["futures_is_final"])
        self.assertNotContains(resp, "정규시즌 최종")

    def test_last_game_day_is_not_yet_final(self):
        """마지막 경기 당일은 그날 결과로 순위가 바뀔 수 있어 확정으로 보지 않는다."""
        resp = self._get(today=self.REGULAR_END)
        self.assertFalse(resp.context["regular_is_final"])

    def test_not_marked_when_schedule_unknown(self):
        resp = self._get(today=date(2026, 10, 20), regular_end=None, futures_end=None)

        self.assertFalse(resp.context["regular_is_final"])
        self.assertFalse(resp.context["futures_is_final"])
        self.assertNotContains(resp, "정규시즌 최종")


@_STATIC_STORAGE
class PostseasonBracketTests(TestCase):
    """순위 페이지의 포스트시즌 대진표 — 1~5위를 5위(와일드카드)부터 1위(한국시리즈 직행) 순으로 놓는다."""

    REGULAR_END = date(2026, 10, 7)
    TEAMS = ["KT", "삼성", "LG", "KIA", "두산", "SSG", "NC"]

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _get(self, today=date(2026, 8, 1), team_count=len(TEAMS)):
        rows = [
            scraping.TeamStandingRow(
                rank=str(i), team=team, games="100", wins="50", losses="50", draws="0",
                win_pct="0.500", games_behind="0", recent_10="5승0무5패", streak="1승",
                home_record="25-0-25", away_record="25-0-25", division="",
            )
            for i, team in enumerate(self.TEAMS[:team_count], start=1)
        ]
        with patch.object(scraping, "fetch_standings_1gun", return_value=rows), \
             patch.object(scraping, "fetch_standings_2gun", return_value=[]), \
             patch.object(scraping, "fetch_regular_season_end_date", return_value=self.REGULAR_END), \
             patch("roster.views.date") as mock_date:
            mock_date.today.return_value = today
            return self.client.get(reverse("roster:standings"))

    def test_top_five_from_fifth_to_first(self):
        resp = self._get()

        self.assertEqual([r.team for r in resp.context["bracket_teams"]], ["두산", "KIA", "LG", "삼성", "KT"])
        self.assertContains(resp, "포스트시즌 대진표")
        self.assertContains(resp, "현재 순위 기준 예상")

    def test_final_standings_label_after_regular_season(self):
        resp = self._get(today=date(2026, 10, 20))

        self.assertContains(resp, "정규시즌 최종 순위 기준")
        self.assertNotContains(resp, "현재 순위 기준 예상")

    def test_hidden_when_standings_incomplete(self):
        resp = self._get(team_count=3)

        self.assertEqual(resp.context["bracket_teams"], [])
        self.assertNotContains(resp, "포스트시즌 대진표")
