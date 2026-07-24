import hmac
import io
from collections import defaultdict

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from . import llm, news, scraping
from .models import Player, RosterEvent, Team

# 방금 어느 리그에서 옮겨왔는지에 따라 반대편 리그 성적을 보여준다.
# 2군->1군 콜업: 콜업 전 2군 성적을 보여준다 / 1군->2군 말소: 말소 전 1군 성적을 보여준다.
_RELATED_LEAGUE = {
    RosterEvent.ACTIVE_1GUN: "2군",
    RosterEvent.OPTIONED_2GUN: "1군",
}

CACHE_TIMEOUT = 60 * 60 * 24  # 리그 순위/매진율 통계는 하루 1회만 스크래핑하면 충분하다.


def _cached_fetch(cache_key, fetch_fn, default=None):
    """KBO 사이트 스크래핑 결과를 하루 동안 캐시한다.

    스크래핑 실패(RequestException) 시에는 캐시에 남기지 않아, 다음 요청에서 바로 재시도한다.
    """
    data = cache.get(cache_key)
    if data is not None:
        return data
    try:
        data = fetch_fn()
    except requests.RequestException:
        return [] if default is None else default
    cache.set(cache_key, data, CACHE_TIMEOUT)
    return data


def _attach_current_status(players):
    """선수 목록의 최신 RosterEvent를 쿼리 1번으로 미리 가져와 current_status 캐시를 채운다.

    Player.current_status는 cached_property라, 여기서 미리 값을 채워두면(non-data descriptor라
    인스턴스에 직접 대입 가능) 이후 뷰/템플릿에서 반복 접근해도 추가 쿼리가 발생하지 않는다.
    미리 채우지 않으면 선수 수만큼 쿼리가 발생하는 N+1 문제가 생긴다.
    """
    players = list(players)
    latest_by_player = {}
    events = (
        RosterEvent.objects.filter(player_id__in=[p.id for p in players])
        .order_by("player_id", "-event_date", "-id")
    )
    for event in events:
        latest_by_player.setdefault(event.player_id, event)
    for player in players:
        player.current_status = latest_by_player.get(player.id)
    return players


def team_list(request):
    teams = Team.objects.prefetch_related("players").all()
    active_type = RosterEvent.ACTIVE_1GUN

    cards = []
    for team in teams:
        players = _attach_current_status(team.players.all())
        active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
        others = [p for p in players if p not in active_players]
        cards.append({"team": team, "active": active_players, "others": others})

    latest_game_date, latest_games = _cached_fetch(
        "latest_game_results", scraping.fetch_latest_game_results, default=(None, [])
    )

    return render(
        request,
        "roster/team_list.html",
        {"cards": cards, "latest_game_date": latest_game_date, "latest_games": latest_games},
    )


def team_detail(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    players = _attach_current_status(team.players.all())
    active_type = RosterEvent.ACTIVE_1GUN
    active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
    others = [p for p in players if p not in active_players]
    return render(
        request,
        "roster/team_detail.html",
        {"team": team, "active_players": active_players, "other_players": others},
    )


def player_detail(request, player_id):
    player = get_object_or_404(Player, pk=player_id)
    events = player.events.select_related("team").all()

    status = player.current_status
    related_league = _RELATED_LEAGUE.get(status.event_type) if status else None
    related_stats = None
    if related_league and player.kbo_player_id:
        try:
            related_stats = scraping.fetch_player_stats(
                player.kbo_player_id, player.position, related_league
            )
        except requests.RequestException:
            related_stats = None

    roster_periods = None
    if player.kbo_player_id:
        try:
            roster_periods = scraping.fetch_roster_periods(player.kbo_player_id, player.position)
        except requests.RequestException:
            roster_periods = None

    return render(
        request,
        "roster/player_detail.html",
        {
            "player": player,
            "events": events,
            "related_league": related_league,
            "related_stats": related_stats,
            "roster_periods": roster_periods,
        },
    )


def recent_events(request):
    events = RosterEvent.objects.select_related("player", "team").all()[:100]
    return render(request, "roster/recent_events.html", {"events": events})


def standings(request):
    standings_1gun = _cached_fetch("standings_1gun", scraping.fetch_standings_1gun)
    rows_2gun = _cached_fetch("standings_2gun", scraping.fetch_standings_2gun)

    standings_2gun = {
        "북부": [r for r in rows_2gun if r.division == "북부"],
        "남부": [r for r in rows_2gun if r.division == "남부"],
    }

    return render(
        request,
        "roster/standings.html",
        {"standings_1gun": standings_1gun, "standings_2gun": standings_2gun},
    )


def attendance_stats(request):
    rows = _cached_fetch("attendance_rows", scraping.fetch_attendance_rows)

    home_rows = [r for r in rows if r.capacity]

    team_stats = []
    for team in sorted({r.home for r in home_rows}):
        team_rows = [r for r in home_rows if r.home == team]

        by_weekday = defaultdict(list)
        by_opponent = defaultdict(list)
        for r in team_rows:
            by_weekday[r.weekday].append(r.sellout_rate)
            by_opponent[r.away].append(r.sellout_rate)

        weekday_stats = [
            {"weekday": wd, "avg_rate": 100 * sum(rates) / len(rates), "games": len(rates)}
            for wd in scraping.WEEKDAY_ORDER
            if (rates := by_weekday.get(wd))
        ]
        opponent_stats = sorted(
            (
                {"opponent": opp, "avg_rate": 100 * sum(rates) / len(rates), "games": len(rates)}
                for opp, rates in by_opponent.items()
            ),
            key=lambda x: -x["avg_rate"],
        )

        team_stats.append(
            {
                "team": team,
                "stadium": team_rows[0].stadium,
                "total_games": len(team_rows),
                "avg_rate": 100 * sum(r.sellout_rate for r in team_rows) / len(team_rows),
                "weekday_stats": weekday_stats,
                "opponent_stats": opponent_stats,
            }
        )

    team_stats.sort(key=lambda x: -x["avg_rate"])

    return render(request, "roster/attendance_stats.html", {"team_stats": team_stats})


def player_news(request, player_id):
    player = get_object_or_404(Player, pk=player_id)

    try:
        articles = news.fetch_player_news(player.name)
    except requests.RequestException:
        articles = []

    summary = llm.summarize_player_news(player.name, articles) if articles else None

    return render(
        request,
        "roster/player_news.html",
        {
            "player": player,
            "articles": articles,
            "summary": summary,
            "llm_model": settings.GROQ_MODEL,
        },
    )


@require_GET
def trigger_sync(request):
    """외부 무료 크론 서비스(cron-job.org 등)가 매일 호출해 sync_roster를 실행시키는 엔드포인트.

    Render 무료 티어에는 Shell/Cron 기능이 없어, DB 접근 없이 URL 호출만으로 동기화를
    트리거할 수 있도록 만들었다. SYNC_SECRET_TOKEN이 설정되지 않았거나 토큰이 일치하지
    않으면 접근을 거부한다.
    """
    expected = settings.SYNC_SECRET_TOKEN
    provided = request.GET.get("token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return HttpResponseForbidden("forbidden")

    buffer = io.StringIO()
    call_command("sync_roster", stdout=buffer, stderr=buffer)
    return HttpResponse(buffer.getvalue() or "sync_roster 완료", content_type="text/plain")
