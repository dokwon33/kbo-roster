from django.urls import path

from . import views

app_name = "roster"

urlpatterns = [
    path("", views.team_list, name="team_list"),
    path("teams/<int:team_id>/", views.team_detail, name="team_detail"),
    path("players/<int:player_id>/", views.player_detail, name="player_detail"),
    path("events/", views.recent_events, name="recent_events"),
    path("standings/", views.standings, name="standings"),
]
