from django.urls import path

from . import views

app_name = "roster"

urlpatterns = [
    path("", views.team_list, name="team_list"),
    path("teams/<int:team_id>/", views.team_detail, name="team_detail"),
    path("players/<int:player_id>/", views.player_detail, name="player_detail"),
    path("players/<int:player_id>/news/", views.player_news, name="player_news"),
    path("players/<int:player_id>/news-summary/", views.player_news_summary, name="player_news_summary"),
    path("sync/", views.trigger_sync, name="trigger_sync"),
    path("stats/", views.stats, name="stats"),
    path("events/", views.recent_events, name="recent_events"),
    path("standings/", views.standings, name="standings"),
    path("attendance/", views.attendance_stats, name="attendance_stats"),
    path("feedback/", views.feedback, name="feedback"),
    path("prediction/", views.prediction, name="prediction"),
    path("prediction/results/", views.prediction_results, name="prediction_results"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("push/unsubscribe/", views.push_unsubscribe, name="push_unsubscribe"),
]
