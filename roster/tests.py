from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import requests

from roster import scraping
from roster.models import Player, RosterEvent, Team
from roster.scraping import AttendanceRow
from roster.views import MAIN_PAGE_STALE_DAYS, ROSTER_STALE_DAYS, _build_callup_headlines

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
    # 업데이트 소식 팝업(모든 페이지에 포함)에도 '정규시즌 최종' 문구가 있어서,
    # 페이지 전체 문구 대신 순위표에 붙는 배지 마크업으로 검사한다.
    FINAL_TAG = '<span class="final-tag">정규시즌 최종</span>'

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
        self.assertNotContains(resp, self.FINAL_TAG, html=True)

    def test_last_game_day_is_not_yet_final(self):
        """마지막 경기 당일은 그날 결과로 순위가 바뀔 수 있어 확정으로 보지 않는다."""
        resp = self._get(today=self.REGULAR_END)
        self.assertFalse(resp.context["regular_is_final"])

    def test_not_marked_when_schedule_unknown(self):
        resp = self._get(today=date(2026, 10, 20), regular_end=None, futures_end=None)

        self.assertFalse(resp.context["regular_is_final"])
        self.assertFalse(resp.context["futures_is_final"])
        self.assertNotContains(resp, self.FINAL_TAG, html=True)


@_STATIC_STORAGE
class PostseasonBracketTests(TestCase):
    """메인 화면 포스트시즌 대진표.

    경기가 시작되기 전에는 정규시즌 순위로 예상 대진을 세우고, 시작한 뒤에는
    실제 결과·남은 일정을 반영한다.
    """

    REGULAR_END = date(2026, 10, 7)
    TEAMS = ["KT", "삼성", "KIA", "LG", "두산", "SSG", "NC"]
    # 업데이트 소식 팝업(모든 페이지에 포함)에도 대진표 소개 문구가 있어서, 페이지 전체 문구 대신
    # 대진표 영역의 마크업으로 검사한다.
    PREDICTED_TAG = '<span class="bracket-basis">현재 순위 기준</span>'
    FINAL_TAG = '<span class="bracket-basis">정규시즌 최종 순위 기준</span>'
    LIVE_TAG = '<span class="bracket-basis">경기 결과 반영</span>'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        cache.set("callup_headlines", (None, None))
        cache.set("latest_game_results", (None, []))

    def _rows(self, team_count):
        return [
            scraping.TeamStandingRow(
                rank=str(i), team=team, games="100", wins="50", losses="50", draws="0",
                win_pct="0.500", games_behind="0", recent_10="5승0무5패", streak="1승",
                home_record="25-0-25", away_record="25-0-25", division="",
            )
            for i, team in enumerate(self.TEAMS[:team_count], start=1)
        ]

    def _get(self, url_name="roster:team_list", today=date(2026, 8, 1),
             team_count=len(TEAMS), games=None):
        with patch.object(scraping, "fetch_standings_1gun", return_value=self._rows(team_count)), \
             patch.object(scraping, "fetch_standings_2gun", return_value=[]), \
             patch.object(scraping, "fetch_postseason_games", return_value=games or []), \
             patch.object(scraping, "fetch_regular_season_end_date", return_value=self.REGULAR_END), \
             patch("roster.views.date") as mock_date:
            mock_date.today.return_value = today
            return self.client.get(reverse(url_name))

    def test_predicted_matchups_before_postseason(self):
        resp = self._get()

        bracket = resp.context["bracket"]
        self.assertEqual([s["key"] for s in bracket], ["wc", "spo", "po", "ks"])
        self.assertEqual([t["team"] for t in bracket[0]["teams"]], ["LG", "두산"])
        self.assertEqual([[t["team"] for t in s["teams"]] for s in bracket[1:]], [["KIA"], ["삼성"], ["KT"]])
        self.assertEqual(
            [s["waiting_for"] for s in bracket[1:]], ["와일드카드 승자", "준플레이오프 승자", "플레이오프 승자"]
        )
        self.assertContains(resp, 'class="bracket-ladder"')
        self.assertContains(resp, self.PREDICTED_TAG)

    def test_final_standings_label_after_regular_season(self):
        resp = self._get(today=date(2026, 10, 20))

        self.assertContains(resp, self.FINAL_TAG)
        self.assertNotContains(resp, self.PREDICTED_TAG)

    def test_hidden_when_standings_incomplete(self):
        resp = self._get(team_count=3)

        self.assertEqual(resp.context["bracket"], [])
        self.assertNotContains(resp, 'class="bracket-ladder"')

    def test_not_on_standings_page(self):
        resp = self._get(url_name="roster:standings")

        self.assertNotIn("bracket", resp.context)
        self.assertNotContains(resp, 'class="bracket-ladder"')

    # --- 실제 경기 결과 반영 ---
    # 2026 포스트시즌을 축약한 상황: WC는 LG가 끝냈고, 준PO는 KIA가 3승으로 올라갔으며,
    # PO는 KIA와 삼성이 1승씩 주고받은 상태에서 3차전이 남아 있다.
    def _game(self, round_key, no, day, away, home, away_score=None, home_score=None,
              stadium="광주", time="18:30"):
        return scraping.PostseasonGame(
            round_key=round_key, game_no=no, game_date=date(2026, 10, day), game_time=time,
            away=away, home=home, away_score=away_score, home_score=home_score, stadium=stadium,
        )

    def _live_games(self):
        return [
            self._game("wc", 1, 14, "두산", "LG", 1, 4, stadium="잠실"),
            self._game("spo", 1, 17, "LG", "KIA", 1, 5),
            self._game("spo", 2, 18, "LG", "KIA", 3, 6),
            self._game("spo", 3, 20, "KIA", "LG", 13, 1, stadium="잠실"),
            self._game("po", 1, 26, "KIA", "삼성", 5, 1, stadium="대구"),
            self._game("po", 2, 27, "KIA", "삼성", 3, 8, stadium="대구"),
            self._game("po", 3, 29, "삼성", "KIA"),
            self._game("ks", None, 31, "PO 승리팀", "KT", stadium="수원"),
        ]

    def _live_bracket(self):
        resp = self._get(today=date(2026, 10, 28), games=self._live_games())
        return resp, {s["key"]: s for s in resp.context["bracket"]}

    def test_completed_rounds_show_actual_winner(self):
        """다음 라운드에 올라간 팀으로 직전 라운드 승자를 판정한다."""
        _, rounds = self._live_bracket()

        wc = {t["team"]: t for t in rounds["wc"]["teams"]}
        self.assertTrue(wc["LG"]["is_winner"])
        self.assertFalse(wc["두산"]["is_winner"])
        self.assertTrue(rounds["wc"]["is_decided"])

        spo = {t["team"]: t for t in rounds["spo"]["teams"]}
        self.assertEqual(spo["KIA"]["wins"], 3)
        self.assertEqual(spo["LG"]["wins"], 0)
        self.assertTrue(spo["KIA"]["is_winner"])

    def test_live_round_shows_record_and_next_game(self):
        """진행 중인 라운드는 승자 없이 전적과 다음 경기 일정을 보여준다."""
        resp, rounds = self._live_bracket()
        po = rounds["po"]

        self.assertFalse(po["is_decided"])
        self.assertEqual({t["team"]: t["wins"] for t in po["teams"]}, {"KIA": 1, "삼성": 1})
        self.assertFalse(any(t["is_winner"] for t in po["teams"]))
        self.assertEqual(po["next_game"].game_date, date(2026, 10, 29))
        # 남은 일정은 날짜와 구장이 화면에 보여야 한다.
        self.assertContains(resp, "10.29")
        self.assertContains(resp, self.LIVE_TAG)

    def test_matchup_replaces_seed_prediction(self):
        """실제 대진이 생기면 순위 기반 예상 대진 대신 그 대진을 쓴다."""
        _, rounds = self._live_bracket()

        # 준PO는 순위만 보면 3위 KIA 혼자 기다리는 칸이지만, 실제로는 LG가 올라와 맞붙었다.
        self.assertEqual(sorted(t["team"] for t in rounds["spo"]["teams"]), ["KIA", "LG"])
        self.assertEqual(rounds["spo"]["waiting_for"], "")

    def test_pending_opponent_still_waits(self):
        """한국시리즈 상대가 'PO 승리팀'으로 들어와도 확정 팀으로 세지 않는다."""
        _, rounds = self._live_bracket()
        ks = rounds["ks"]

        self.assertEqual([t["team"] for t in ks["teams"]], ["KT"])
        self.assertEqual(ks["waiting_for"], "플레이오프 승자")
        self.assertFalse(ks["is_decided"])

    def test_korean_series_winner_from_clinch(self):
        """마지막 라운드는 다음 라운드가 없으므로 4선승으로 우승을 판정한다."""
        games = [self._game("ks", n, 20 + n, "KIA", "KT", 5, 1, stadium="수원") for n in range(1, 5)]
        resp = self._get(today=date(2026, 11, 5), games=games)
        ks = {s["key"]: s for s in resp.context["bracket"]}["ks"]

        self.assertTrue(ks["is_decided"])
        self.assertEqual([t["team"] for t in ks["teams"] if t["is_winner"]], ["KIA"])
        self.assertIsNone(ks["next_game"])


class CallupHeadlineTests(TestCase):
    """홈 화면 콜업 헤드라인 — 후보별 성적 요청을 병렬로 보내도 선정 결과는 순차 처리와 같아야 한다."""

    def setUp(self):
        cache.clear()
        self.team = Team.objects.create(name="KT")
        today = timezone.now().date()
        self.pitcher_a = self._callup("투수A", "투", "P1", today)
        self.pitcher_b = self._callup("투수B", "투", "P2", today - timedelta(days=1))
        self.hitter_a = self._callup("타자A", "내", "H1", today - timedelta(days=2))
        self.hitter_b = self._callup("타자B", "외", "H2", today - timedelta(days=3))
        self.pitcher_stats = {
            "P1": {"이닝": "10 1/3", "평균자책점": "3.50"},
            "P2": {"이닝": "12", "평균자책점": "2.10"},
        }
        self.hitter_stats = {
            "H1": {"타수": "40", "출루율": "0.350", "장타율": "0.450"},
            "H2": {"타수": "50", "출루율": "0.400", "장타율": "0.500"},
        }

    def _callup(self, name, position, kbo_id, event_date):
        player = Player.objects.create(name=name, team=self.team, position=position, kbo_player_id=kbo_id)
        RosterEvent.objects.create(
            player=player, team=self.team, event_date=event_date, event_type=RosterEvent.ACTIVE_1GUN
        )
        return player

    def _patch_stats(self, failing_ids=()):
        def fake(stats_by_id):
            def fetch(player_id, league):
                if player_id in failing_ids:
                    raise requests.RequestException("boom")
                return stats_by_id.get(player_id)

            return fetch

        return (
            patch.object(scraping, "fetch_pitcher_stats", side_effect=fake(self.pitcher_stats)),
            patch.object(scraping, "fetch_hitter_stats", side_effect=fake(self.hitter_stats)),
        )

    def test_picks_best_era_and_ops(self):
        pitcher_patch, hitter_patch = self._patch_stats()
        with pitcher_patch, hitter_patch:
            hitter, pitcher = _build_callup_headlines()

        self.assertEqual(pitcher["player"], self.pitcher_b)
        self.assertEqual(hitter["player"], self.hitter_b)
        self.assertAlmostEqual(hitter["ops"], 0.9)

    def test_failed_request_skips_only_that_player(self):
        pitcher_patch, hitter_patch = self._patch_stats(failing_ids={"P2", "H2"})
        with pitcher_patch, hitter_patch:
            hitter, pitcher = _build_callup_headlines()

        self.assertEqual(pitcher["player"], self.pitcher_a)
        self.assertEqual(hitter["player"], self.hitter_a)

    def test_tie_keeps_most_recent_callup(self):
        """동점이면 더 최근에 콜업된(후보 순서가 앞선) 선수가 뽑힌다 — 병렬화 전과 같은 동작."""
        self.pitcher_stats["P2"] = dict(self.pitcher_stats["P1"])
        pitcher_patch, hitter_patch = self._patch_stats()
        with pitcher_patch, hitter_patch:
            _, pitcher = _build_callup_headlines()

        self.assertEqual(pitcher["player"], self.pitcher_a)

    @override_settings(SYNC_SECRET_TOKEN="secret")
    def test_sync_prewarms_headline_cache(self):
        pitcher_patch, hitter_patch = self._patch_stats()
        with pitcher_patch, hitter_patch, patch("roster.views.call_command"):
            resp = self.client.get(reverse("roster:trigger_sync"), {"token": "secret"})

        self.assertEqual(resp.status_code, 200)
        hitter, pitcher = cache.get("callup_headlines")
        self.assertEqual(pitcher["player"], self.pitcher_b)
        self.assertEqual(hitter["player"], self.hitter_b)

    @override_settings(SYNC_SECRET_TOKEN="secret")
    def test_sync_succeeds_even_if_headline_refresh_fails(self):
        with patch("roster.views.call_command"), patch(
            "roster.views._build_callup_headlines", side_effect=RuntimeError("boom")
        ), self.assertLogs("roster.views", level="ERROR"):
            resp = self.client.get(reverse("roster:trigger_sync"), {"token": "secret"})

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(cache.get("callup_headlines"))


class PostseasonScheduleParsingTests(TestCase):
    """KBO 일정표 응답 파싱 — 점수/플레이스홀더/라운드 표기가 섞여 들어온다."""

    def _row(self, day, time, play, stadium, note):
        cells = [day, time, play, "", "", "", "", stadium, note]
        return {"row": [{"Text": c} for c in cells]}

    def _payload(self, *rows):
        return {"rows": list(rows)}

    def test_parses_finished_game(self):
        payload = self._payload(self._row(
            "10.14(수)", "<b>18:30</b>",
            '<span>두산</span><em><span class="lose">1</span><span>vs</span>'
            '<span class="win">4</span></em><span>LG</span>',
            "잠실", "WC 1차전",
        ))
        game, = scraping._parse_postseason_rows(payload, 2026)

        self.assertEqual(
            (game.round_key, game.game_no, game.away, game.home), ("wc", 1, "두산", "LG")
        )
        self.assertEqual((game.away_score, game.home_score), (1, 4))
        self.assertEqual(game.game_date, date(2026, 10, 14))
        self.assertEqual(game.stadium, "잠실")
        self.assertEqual(game.winner, "LG")
        self.assertEqual(game.result_label, "LG 4:1")

    def test_parses_scheduled_game_without_score(self):
        payload = self._payload(self._row(
            "10.29(목)", "<b>18:30</b>",
            "<span>삼성</span><em><span>vs</span></em><span>KIA</span>",
            "광주", "PO 3차전",
        ))
        game, = scraping._parse_postseason_rows(payload, 2026)

        self.assertFalse(game.is_played)
        self.assertIsNone(game.winner)
        self.assertEqual(game.game_time, "18:30")

    def test_unlabelled_rows_fall_back_to_korean_series(self):
        """한국시리즈는 대진 확정 전까지 비고란이 비어 있고 상대가 'PO 승리팀'으로 들어온다."""
        payload = self._payload(self._row(
            "11.02(월)", "<b>18:30</b>",
            "<span>PO<br/>승리팀</span><em><span>vs</span></em><span>KT</span>",
            "수원", "",
        ))
        game, = scraping._parse_postseason_rows(payload, 2026)

        self.assertEqual(game.round_key, "ks")
        self.assertEqual(game.away, "PO 승리팀")
        self.assertTrue(game.has_pending_team)
        self.assertTrue(scraping.is_pending_team(game.away))
        self.assertFalse(scraping.is_pending_team(game.home))

    def test_skips_travel_days_and_carries_date_forward(self):
        """'이동일' 행은 경기가 없고, 같은 날 두 번째 경기는 날짜 칸이 비어 있다."""
        payload = self._payload(
            self._row("10.15(목)", "", "", "", "이동일"),
            self._row("10.17(토)", "<b>14:00</b>",
                      '<span>LG</span><em><span class="lose">1</span><span>vs</span>'
                      '<span class="win">5</span></em><span>KIA</span>', "광주", "준PO 1차전"),
            self._row("", "<b>18:30</b>",
                      "<span>LG</span><em><span>vs</span></em><span>KIA</span>", "광주", "준PO 2차전"),
        )
        games = scraping._parse_postseason_rows(payload, 2026)

        self.assertEqual([g.game_no for g in games], [1, 2])
        self.assertEqual([g.game_date for g in games], [date(2026, 10, 17)] * 2)
        self.assertEqual([g.round_key for g in games], ["spo", "spo"])


class TransactionParsingTests(TestCase):
    """KBO 선수 이동 공시 응답 파싱."""

    def _payload(self, rows, total=None):
        return {
            "rows": [{"row": [{"Text": c} for c in r]} for r in rows],
            "totalCnt": len(rows) if total is None else total,
        }

    def test_splits_player_name_and_position(self):
        payload = self._payload([["2026-10-26", "자유계약선수", "KIA", "파노니(투수)", ""]])
        tx, = scraping._parse_transaction_rows(payload)

        self.assertEqual(tx.tx_date, date(2026, 10, 26))
        self.assertEqual((tx.kind, tx.team), ("자유계약선수", "KIA"))
        self.assertEqual((tx.player_name, tx.position), ("파노니", "투수"))

    def test_keeps_trade_direction_note(self):
        payload = self._payload([["2025-11-25", "트레이드", "삼성", "박세혁(포수)", "NC→삼성"]])
        tx, = scraping._parse_transaction_rows(payload)
        self.assertEqual(tx.note, "NC→삼성")

    def test_skips_rows_without_valid_date(self):
        payload = self._payload([
            ["", "자유계약선수", "KIA", "홍길동(투수)", ""],
            ["2026-10-26", "웨이버", "LG", "김철수(포수)", ""],
        ])
        rows = scraping._parse_transaction_rows(payload)
        self.assertEqual([r.player_name for r in rows], ["김철수"])

    def test_handles_name_without_position(self):
        payload = self._payload([["2026-10-26", "개명", "LG", "김철수", ""]])
        tx, = scraping._parse_transaction_rows(payload)
        self.assertEqual((tx.player_name, tx.position), ("김철수", ""))

    def test_stops_paging_once_total_reached(self):
        payload = self._payload([["2026-10-26", "웨이버", "LG", "김철수(포수)", ""]], total=1)
        with patch.object(scraping.requests, "post") as post:
            post.return_value.json.return_value = payload
            post.return_value.raise_for_status.return_value = None
            rows = scraping.fetch_transactions(2026)

        self.assertEqual(len(rows), 1)
        self.assertEqual(post.call_count, 1)
        # bdSc를 빈 문자열로 보내면 서버가 500을 내려준다.
        self.assertEqual(post.call_args.kwargs["data"]["bdSc"], scraping.ALL_TRANSACTION_KINDS)


@_STATIC_STORAGE
class TransactionPageTests(TestCase):
    """이적시장 타임라인 — 비시즌에 유일하게 갱신되는 화면."""

    ROWS = [
        ("2026-11-20", "자유계약선수", "KIA", "방출선수(투수)", ""),
        ("2026-11-20", "부상자 명단", "LG", "부상선수(포수)", "10일"),
        ("2026-11-18", "트레이드", "삼성", "이적선수(내야수)", "NC→삼성"),
        ("2026-11-15", "등번호 변경", "KT", "기타선수(외야수)", "11→22"),
    ]

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _get(self, query="", today=date(2026, 11, 25)):
        rows = scraping._parse_transaction_rows(
            {"rows": [{"row": [{"Text": c} for c in r]} for r in self.ROWS], "totalCnt": len(self.ROWS)}
        )
        rows.sort(key=lambda t: t.tx_date, reverse=True)
        with patch.object(scraping, "fetch_transactions", return_value=rows) as fetch, \
             patch("roster.views.date") as mock_date:
            mock_date.today.return_value = today
            resp = self.client.get(reverse("roster:transactions") + query)
        return resp, fetch

    def _kinds(self, resp):
        return [r.kind for day in resp.context["days"] for r in day["rows"]]

    def test_defaults_to_market_moves_only(self):
        """부상자 명단이 한 시즌 200건이 넘어, 기본 화면에서는 이적 소식만 보여준다."""
        resp, _ = self._get()

        self.assertEqual(resp.context["group"], "market")
        self.assertEqual(self._kinds(resp), ["자유계약선수", "트레이드"])
        self.assertContains(resp, "NC→삼성")

    def test_groups_rows_by_date(self):
        resp, _ = self._get("?group=all")

        self.assertEqual(
            [(d["date"], len(d["rows"])) for d in resp.context["days"]],
            [(date(2026, 11, 20), 2), (date(2026, 11, 18), 1), (date(2026, 11, 15), 1)],
        )

    def test_group_filter_and_counts(self):
        resp, _ = self._get("?group=injury")

        self.assertEqual(self._kinds(resp), ["부상자 명단"])
        self.assertEqual(
            {g["key"]: g["count"] for g in resp.context["groups"]},
            {"market": 2, "injury": 1, "etc": 1},
        )

    def test_unknown_group_falls_back_to_default(self):
        resp, _ = self._get("?group=없는그룹")
        self.assertEqual(resp.context["group"], "market")

    def test_season_selection(self):
        _, fetch = self._get("?year=2024")
        self.assertEqual(fetch.call_args.args, (2024,))

    def test_invalid_or_future_season_falls_back_to_this_year(self):
        for query in ("?year=abc", "?year=2099", "?year=1999"):
            cache.clear()  # 같은 시즌으로 떨어지므로 캐시를 비워야 매번 다시 조회한다.
            _, fetch = self._get(query)
            self.assertEqual(fetch.call_args.args, (2026,), query)

    def test_links_only_players_present_in_db(self):
        team = Team.objects.create(name="KIA")
        known = Player.objects.create(name="방출선수", team=team, position="투")

        resp, _ = self._get()

        self.assertEqual(resp.context["player_ids"], {"방출선수": known.id})
        self.assertContains(resp, reverse("roster:player_detail", args=[known.id]))
