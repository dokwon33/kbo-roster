from collections import defaultdict

import requests
from django.conf import settings
from django.shortcuts import get_object_or_404, render

from . import llm, news, scraping
from .models import Player, RosterEvent, Team

# 방금 어느 리그에서 옮겨왔는지에 따라 반대편 리그 성적을 보여준다.
# 2군->1군 콜업: 콜업 전 2군 성적을 보여준다 / 1군->2군 말소: 말소 전 1군 성적을 보여준다.
_RELATED_LEAGUE = {
    RosterEvent.ACTIVE_1GUN: "2군",
    RosterEvent.OPTIONED_2GUN: "1군",
}


def team_list(request):
    teams = Team.objects.prefetch_related("players").all()
    active_type = RosterEvent.ACTIVE_1GUN

    cards = []
    for team in teams:
        players = list(team.players.all())
        active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
        others = [p for p in players if p not in active_players]
        cards.append({"team": team, "active": active_players, "others": others})

    try:
        latest_game_date, latest_games = scraping.fetch_latest_game_results()
    except requests.RequestException:
        latest_game_date, latest_games = None, []

    return render(
        request,
        "roster/team_list.html",
        {"cards": cards, "latest_game_date": latest_game_date, "latest_games": latest_games},
    )


def team_detail(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    players = team.players.all()
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
    try:
        standings_1gun = scraping.fetch_standings_1gun()
    except requests.RequestException:
        standings_1gun = []

    try:
        rows_2gun = scraping.fetch_standings_2gun()
    except requests.RequestException:
        rows_2gun = []

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
    try:
        rows = scraping.fetch_attendance_rows()
    except requests.RequestException:
        rows = []

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
            "ollama_model": settings.OLLAMA_MODEL,
        },
    )
