from django.shortcuts import get_object_or_404, render

from .models import Player, RosterEvent, Team


def team_list(request):
    teams = Team.objects.prefetch_related("players").all()
    active_type = RosterEvent.ACTIVE_1GUN

    cards = []
    for team in teams:
        players = list(team.players.all())
        active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
        others = [p for p in players if p not in active_players]
        cards.append({"team": team, "active": active_players, "others": others})

    return render(request, "roster/team_list.html", {"cards": cards})


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
    return render(request, "roster/player_detail.html", {"player": player, "events": events})


def recent_events(request):
    events = RosterEvent.objects.select_related("player", "team").all()[:100]
    return render(request, "roster/recent_events.html", {"events": events})
