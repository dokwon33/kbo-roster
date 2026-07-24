import hmac
import io
from collections import defaultdict
from datetime import timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
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


NEWS_SUMMARY_TTL = timedelta(hours=6)


def _get_news_summary(player):
    """선수 뉴스 AI 요약을 가져온다. 캐시가 없거나 오래됐으면 새로 생성해 Player에 저장한다.

    네이버 뉴스 API/Groq LLM 호출은 비용·응답 시간이 들기 때문에, 방문할 때마다 새로
    호출하지 않고 결과를 DB에 캐싱해 NEWS_SUMMARY_TTL 동안 재사용한다. 조회가 실패해도
    기존 캐시는 유지하고 재시도 시각만 갱신해, 장애 중에 매 요청마다 재시도하지 않는다.
    """
    is_stale = (
        player.news_summary_updated_at is None
        or timezone.now() - player.news_summary_updated_at > NEWS_SUMMARY_TTL
    )
    if not is_stale:
        return player.news_summary, player.news_articles_cache

    try:
        articles = news.fetch_player_news(player.name)
    except requests.RequestException:
        articles = None

    if articles is not None:
        player.news_summary = (llm.summarize_player_news(player.name, articles) if articles else None) or ""
        player.news_articles_cache = [
            {"title": a.title, "description": a.description, "link": a.link, "pub_date": a.pub_date}
            for a in articles
        ]
    player.news_summary_updated_at = timezone.now()
    player.save(update_fields=["news_summary", "news_articles_cache", "news_summary_updated_at"])

    return player.news_summary, player.news_articles_cache


CALLUP_HEADLINE_WINDOW = timedelta(days=7)
CALLUP_MIN_AB = 10  # 표본이 너무 적으면 극단값(예: 1타수 1안타 OPS 2.000)이 뽑히는 걸 방지
CALLUP_MIN_IP = 5.0


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_innings(ip_str):
    """KBO 이닝 표기('9 2/3'처럼 분수가 붙는 경우 포함)를 실수로 변환한다."""
    if not ip_str:
        return None
    parts = ip_str.strip().split()
    try:
        innings = float(parts[0])
    except ValueError:
        return None
    if len(parts) > 1 and "/" in parts[1]:
        num, _, den = parts[1].partition("/")
        try:
            innings += float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass
    return innings


def _recent_callup_players():
    """최근 CALLUP_HEADLINE_WINDOW 기간 내 1군에 콜업됐고, 그 이후 다시 말소되지 않아
    지금도 1군인 선수 목록을 반환한다."""
    since = timezone.now().date() - CALLUP_HEADLINE_WINDOW
    events = (
        RosterEvent.objects.filter(event_type=RosterEvent.ACTIVE_1GUN, event_date__gte=since)
        .select_related("player")
        .order_by("player_id", "-event_date", "-id")
    )
    players = []
    seen = set()
    for event in events:
        player = event.player
        if player.id in seen or not player.kbo_player_id:
            continue
        seen.add(player.id)
        latest = player.current_status
        if latest is None or latest.id != event.id:
            continue
        players.append(player)
    return players


def _build_callup_headlines():
    """최근 콜업된 선수들의 2군 성적(OPS/평균자책점)을 바탕으로 기대되는 타자/투수 1명씩 뽑는다.

    비교 대상이 최근 콜업 선수들뿐이라, 리그 전체 평균 같은 별도 기준 없이도 상대적으로
    비교 가능하다. KBO 공식 2군 성적 페이지 값(출루율+장타율=OPS, 평균자책점)만 그대로 쓴다.
    """
    hitter_best = None
    pitcher_best = None
    for player in _recent_callup_players():
        try:
            if player.position == "투":
                stats = scraping.fetch_pitcher_stats(player.kbo_player_id, "2군")
                if not stats:
                    continue
                ip = _parse_innings(stats.get("이닝"))
                era = _parse_float(stats.get("평균자책점"))
                if ip is None or era is None or ip < CALLUP_MIN_IP:
                    continue
                if pitcher_best is None or era < pitcher_best["era"]:
                    pitcher_best = {"player": player, "era": era, "stats": stats}
            else:
                stats = scraping.fetch_hitter_stats(player.kbo_player_id, "2군")
                if not stats:
                    continue
                ab = _parse_float(stats.get("타수"))
                obp = _parse_float(stats.get("출루율"))
                slg = _parse_float(stats.get("장타율"))
                if ab is None or obp is None or slg is None or ab < CALLUP_MIN_AB:
                    continue
                ops = obp + slg
                if hitter_best is None or ops > hitter_best["ops"]:
                    hitter_best = {"player": player, "ops": ops, "stats": stats}
        except requests.RequestException:
            continue
    return hitter_best, pitcher_best


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
    hitter_headline, pitcher_headline = _cached_fetch(
        "callup_headlines", _build_callup_headlines, default=(None, None)
    )

    return render(
        request,
        "roster/team_list.html",
        {
            "cards": cards,
            "latest_game_date": latest_game_date,
            "latest_games": latest_games,
            "hitter_headline": hitter_headline,
            "pitcher_headline": pitcher_headline,
        },
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

    news_summary, news_articles = _get_news_summary(player)

    return render(
        request,
        "roster/player_detail.html",
        {
            "player": player,
            "events": events,
            "related_league": related_league,
            "related_stats": related_stats,
            "roster_periods": roster_periods,
            "news_summary": news_summary,
            "news_articles": news_articles,
            "llm_model": settings.GROQ_MODEL,
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
    summary, articles = _get_news_summary(player)

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
