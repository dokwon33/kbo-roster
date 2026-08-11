import hmac
import io
import json
import logging
from collections import defaultdict
from datetime import date, time as dt_time, timedelta
from pathlib import Path

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from . import llm, news, scraping
from .models import (
    DailyStat,
    Feedback,
    Player,
    PredictionPick,
    PredictionSubmission,
    PushSubscription,
    RosterEvent,
    Team,
)
from .templatetags.roster_extras import RECENT_EVENT_DAYS

logger = logging.getLogger(__name__)

# 방금 어느 리그에서 옮겨왔는지에 따라 반대편 리그 성적을 보여준다.
# 2군->1군 콜업: 콜업 전 2군 성적을 보여준다 / 1군->2군 말소: 말소 전 1군 성적을 보여준다.
_RELATED_LEAGUE = {
    RosterEvent.ACTIVE_1GUN: "2군",
    RosterEvent.OPTIONED_2GUN: "1군",
}

CACHE_TIMEOUT = 60 * 60 * 24  # 매진율 통계 등은 하루 1회만 스크래핑하면 충분하다.
# 경기 결과는 보통 밤 11시 전에 갈리기 때문에, 하루 캐시를 그대로 쓰면 마지막 방문 시점 기준으로
# 다음날 같은 시각까지 어제 결과가 남아있을 수 있다. 그래서 훨씬 짧은 주기로 따로 캐시한다.
GAME_RESULTS_CACHE_TIMEOUT = 60 * 15


def _cached_fetch(cache_key, fetch_fn, default=None, timeout=CACHE_TIMEOUT):
    """KBO 사이트 스크래핑 결과를 일정 시간 동안 캐시한다.

    스크래핑 실패(RequestException) 시에는 캐시에 남기지 않아, 다음 요청에서 바로 재시도한다.
    """
    data = cache.get(cache_key)
    if data is not None:
        return data
    try:
        data = fetch_fn()
    except requests.RequestException:
        return [] if default is None else default
    cache.set(cache_key, data, timeout)
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


ROSTER_STALE_DAYS = 30
MAIN_PAGE_STALE_DAYS = 14  # 메인 페이지("구단별 1군/2군 변동")는 더 최근 변동만 보여주도록 팀 상세보다 짧게 잡음


def _drop_stale(players, days=ROSTER_STALE_DAYS):
    """마지막 변동으로부터 days일이 지난 선수는 명단에서 제외한다.

    로스터 명단은 '최근 변동 사항'을 보여주는 목적이라, 오래 방치된 선수가
    실제 소속과 무관하게 계속 노출되는 것을 막는다.
    """
    cutoff = timezone.now().date() - timedelta(days=days)
    return [p for p in players if p.current_status and p.current_status.event_date >= cutoff]


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
        team_name = player.team.name if player.team else None
        summary = llm.summarize_player_news(player.name, team_name, articles) if articles else None
        player.news_summary = summary or ""
        player.news_articles_cache = [
            {"title": a.title, "description": a.description, "link": a.link, "pub_date": a.pub_date}
            for a in articles
        ]
    player.news_summary_updated_at = timezone.now()
    player.save(update_fields=["news_summary", "news_articles_cache", "news_summary_updated_at"])

    return player.news_summary, player.news_articles_cache


CALLUP_CANDIDATE_LIMIT = 20  # 날짜(일수) 컷오프 대신 "가장 최근 콜업 N명" 방식 - 콜업이 뜸한 시기에도 헤드라인이 통째로 비지 않도록 함
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
    """가장 최근에 1군 콜업됐고, 그 이후 다시 말소되지 않아 지금도 1군인 선수 중
    콜업 시점이 최신순인 상위 CALLUP_CANDIDATE_LIMIT명을 반환한다.

    날짜(일수) 컷오프 대신 "최근 콜업 N명" 방식을 쓰는 이유: 콜업이 뜸한 시기엔
    고정된 날짜 창 안에 아무도 안 들어와서 헤드라인이 통째로 비어버리는데, 이 방식은
    활동이 적을 때 자연스럽게 더 과거까지 훑어서 항상 마지막으로 콜업된 선수들을 보여준다.
    """
    events = (
        RosterEvent.objects.filter(event_type=RosterEvent.ACTIVE_1GUN)
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
        players.append((event.event_date, event.id, player))

    players.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [player for _, _, player in players[:CALLUP_CANDIDATE_LIMIT]]


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
    team_ids_by_name = {}
    for team in teams:
        players = _drop_stale(_attach_current_status(team.players.all()), days=MAIN_PAGE_STALE_DAYS)
        active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
        others = [p for p in players if p not in active_players]
        recent_cutoff = timezone.now().date() - timedelta(days=RECENT_EVENT_DAYS)
        has_recent = any(p.current_status.event_date >= recent_cutoff for p in players if p.current_status)
        cards.append({"team": team, "active": active_players, "others": others, "has_recent": has_recent})
        team_ids_by_name[team.name] = team.id

    latest_game_date, latest_games = _cached_fetch(
        "latest_game_results",
        scraping.fetch_latest_game_results,
        default=(None, []),
        timeout=GAME_RESULTS_CACHE_TIMEOUT,
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
            "team_ids_by_name": team_ids_by_name,
        },
    )


@ensure_csrf_cookie
def team_detail(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    players = _drop_stale(_attach_current_status(team.players.all()))
    active_type = RosterEvent.ACTIVE_1GUN
    active_players = [p for p in players if p.current_status and p.current_status.event_type == active_type]
    others = [p for p in players if p not in active_players]
    return render(
        request,
        "roster/team_detail.html",
        {
            "team": team,
            "active_players": active_players,
            "other_players": others,
            "vapid_public_key": settings.VAPID_PUBLIC_KEY,
        },
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

    # 네이버 뉴스/Groq LLM 호출은 몇 초씩 걸릴 수 있어, 상세 페이지 렌더링을 막지 않도록
    # 여기서는 이미 캐시된 값만 즉시 보여주고, 최신화(6시간 지났으면 재생성)는
    # player_news_summary 뷰를 JS로 비동기 호출해 처리한다.
    return render(
        request,
        "roster/player_detail.html",
        {
            "player": player,
            "events": events,
            "related_league": related_league,
            "related_stats": related_stats,
            "roster_periods": roster_periods,
            "news_summary": player.news_summary,
            "news_articles": player.news_articles_cache,
            "news_summary_updated_at": player.news_summary_updated_at,
            "llm_model": settings.GROQ_MODEL,
        },
    )


def recent_events(request):
    cutoff = timezone.now().date() - timedelta(days=ROSTER_STALE_DAYS)
    events = RosterEvent.objects.select_related("player", "team").filter(event_date__gte=cutoff)[:100]
    return render(request, "roster/recent_events.html", {"events": events})


def standings(request):
    # 캐시 키에 날짜를 포함해, sync_roster(크론)가 실패하거나 건너뛰어도(경기 없는 날 포함)
    # 날짜가 바뀌면 자동으로 새로 스크래핑하게 한다. 같은 날 안에서는 그대로 재사용해 부담을 줄인다.
    today = date.today()
    standings_1gun = _cached_fetch(f"standings_1gun_{today}", scraping.fetch_standings_1gun)
    rows_2gun = _cached_fetch(f"standings_2gun_{today}", scraping.fetch_standings_2gun)

    standings_2gun = {
        "북부": [r for r in rows_2gun if r.division == "북부"],
        "남부": [r for r in rows_2gun if r.division == "남부"],
    }

    team_ids_by_name = dict(Team.objects.values_list("name", "id"))

    return render(
        request,
        "roster/standings.html",
        {
            "standings_1gun": standings_1gun,
            "standings_2gun": standings_2gun,
            "team_ids_by_name": team_ids_by_name,
        },
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
def player_news_summary(request, player_id):
    """선수 상세 페이지가 JS로 비동기 호출하는 뉴스 요약 API.

    캐시가 신선하면 DB에 저장된 값을 그대로 반환하고, 오래됐으면 이 요청 안에서
    새로 생성한다 — 페이지가 이미 그려진 뒤 백그라운드에서 호출되므로 느려도
    화면 렌더링을 막지 않는다.
    """
    player = get_object_or_404(Player, pk=player_id)
    summary, articles = _get_news_summary(player)
    return JsonResponse({
        "summary": summary,
        "article_count": len(articles or []),
        "llm_model": settings.GROQ_MODEL,
    })


@require_http_methods(["GET", "POST"])
def feedback(request):
    sent = False
    if request.method == "POST":
        # 스팸 봇 방지용 허니팟 — 사람 눈에는 안 보이는 필드라 채워져 있으면 봇으로 간주하고 조용히 무시한다.
        if request.POST.get("website"):
            return render(request, "roster/feedback.html", {"sent": True})

        content = request.POST.get("content", "").strip()
        contact = request.POST.get("contact", "").strip()
        if content:
            record = Feedback.objects.create(content=content, contact=contact)
            try:
                send_mail(
                    subject="[KBO 로스터 트래커] 새 의견이 도착했습니다",
                    message=f"{content}\n\n연락처: {contact or '(미입력)'}",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[settings.FEEDBACK_RECIPIENT_EMAIL],
                    fail_silently=False,
                )
                record.email_sent = True
                record.save(update_fields=["email_sent"])
            except Exception:
                # 메일 발송(SMTP 설정 오류 등)에 실패해도 의견 자체는 DB에 남아 있으니 유실되지 않는다.
                # 다만 원인을 알 수 있게 로그는 남긴다(Render 로그 스트림에서 확인 가능).
                logger.exception("피드백 메일 발송 실패 (feedback id=%s)", record.id)
            sent = True

    return render(request, "roster/feedback.html", {"sent": sent})


# 승부예측 이벤트 진행 기간. 1회성 캠페인이라 별도 모델 없이 상수로 관리한다.
PREDICTION_START_DATE = date(2026, 8, 7)
PREDICTION_END_DATE = date(2026, 8, 13)
PREDICTION_GAMES_CACHE_TIMEOUT = 60 * 5


def _prediction_window(now):
    """오늘 승부예측 참여가 가능한 시간대인지 판단한다.

    평일은 08:00~18:00, 주말(경기 시작이 이른 토/일)은 08:00~14:00까지만 받는다.
    당일 경기 시작 전에 예측을 마감하기 위한 시간대라 요일마다 다르다.
    """
    is_weekend = now.weekday() >= 5
    end_hour = 14 if is_weekend else 18
    return dt_time(8, 0) <= now.time() < dt_time(end_hour, 0)


def _pick_status(game, picked_team):
    """제출된 픽 하나가 지금 시점 기준으로 어떤 상태인지 판정한다.

    아직 못 찾은 경기(game=None)나 시작 전 경기는 "pending", 진행 중이면 "live"(현재 스코어 표시),
    종료면 "correct"/"wrong", 취소면 "cancelled", 동점으로 끝났으면(우천 콜드 등) "draw".
    """
    if game is None:
        return "pending"
    if game.is_cancelled:
        return "cancelled"
    if game.is_ended:
        if game.away_score == game.home_score:
            return "draw"
        winner = game.away_team if game.away_score > game.home_score else game.home_team
        return "correct" if picked_team == winner else "wrong"
    if game.state == "1":
        return "pending"
    return "live"


def _render_prediction(request, state, **extra):
    context = {
        "state": state,
        "event_start": PREDICTION_START_DATE,
        "event_end": PREDICTION_END_DATE,
        **extra,
    }
    return render(request, "roster/prediction.html", context)


@require_http_methods(["GET", "POST"])
def prediction(request):
    now = timezone.localtime()
    today = now.date()

    if not (PREDICTION_START_DATE <= today <= PREDICTION_END_DATE):
        return _render_prediction(request, "OUT_OF_PERIOD")

    games = _cached_fetch(
        f"prediction_games_{today.isoformat()}",
        lambda: scraping.fetch_games_for_date(today),
        timeout=PREDICTION_GAMES_CACHE_TIMEOUT,
    )
    if not games:
        return _render_prediction(request, "NO_GAMES")

    window_open = _prediction_window(now)

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    existing = PredictionSubmission.objects.filter(date=today, session_key=session_key).first()
    if existing:
        games_by_id = {g.game_id: g for g in games}
        pick_rows = []
        for pick in existing.picks.all():
            g = games_by_id.get(pick.game_id)
            pick_rows.append({"pick": pick, "game": g, "status": _pick_status(g, pick.picked_team)})
        return _render_prediction(request, "ALREADY_SUBMITTED", submission=existing, pick_rows=pick_rows)

    if request.method == "POST":
        # 스팸 봇 방지용 허니팟 — feedback 뷰와 동일한 패턴.
        if request.POST.get("website"):
            return _render_prediction(request, "SUBMITTED")

        if not window_open:
            return _render_prediction(request, "WINDOW_CLOSED", games=games)

        instagram_id = request.POST.get("instagram_id", "").strip().lstrip("@")
        picks_by_game = {g.game_id: request.POST.get(f"pick_{g.game_id}", "") for g in games}
        valid_picks = all(
            picks_by_game[g.game_id] in (g.away_team, g.home_team) for g in games
        )

        if not instagram_id or not valid_picks:
            return _render_prediction(request, "FORM_ERROR", games=games)

        try:
            with transaction.atomic():
                submission = PredictionSubmission.objects.create(
                    date=today, instagram_id=instagram_id, session_key=session_key
                )
                PredictionPick.objects.bulk_create(
                    PredictionPick(
                        submission=submission,
                        game_id=g.game_id,
                        away_team=g.away_team,
                        home_team=g.home_team,
                        picked_team=picks_by_game[g.game_id],
                    )
                    for g in games
                )
        except IntegrityError:
            return _render_prediction(request, "DUPLICATE", games=games)

        return _render_prediction(request, "SUBMITTED")

    state = "OPEN" if window_open else "WINDOW_CLOSED"
    return _render_prediction(request, state, games=games)


def _winners_by_date(target_date):
    """해당 날짜 실제 경기 결과에서 game_id → 승리팀 이름 맵을 만든다. 아직 안 끝난/취소된 경기는 제외."""
    games = _cached_fetch(
        f"prediction_actual_results_{target_date.isoformat()}",
        lambda: scraping.fetch_games_for_date(target_date),
        timeout=PREDICTION_GAMES_CACHE_TIMEOUT,
    )
    winners = {}
    for g in games:
        if g.is_ended and g.away_score != g.home_score:
            winners[g.game_id] = g.away_team if g.away_score > g.home_score else g.home_team
    return winners


@require_GET
def prediction_results(request):
    """승부예측 응모 목록/순위 조회. 개발자 확인(추첨)용이라 /sync/와 동일한 토큰으로 보호한다.

    순위는 인스타그램 아이디 기준으로 (같은 아이디로 여러 날 참여 가능하므로) 전체 기간 맞춘 개수를
    합산해 정하고, 동점이면 먼저 참여한 사람을 우선한다.
    """
    expected = settings.SYNC_SECRET_TOKEN
    provided = request.GET.get("token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return HttpResponseForbidden("forbidden")

    submissions = list(PredictionSubmission.objects.prefetch_related("picks").all())

    winners_cache = {}
    totals = {}  # instagram_id -> {"correct": int, "first_created_at": datetime}
    for submission in submissions:
        if submission.date not in winners_cache:
            winners_cache[submission.date] = _winners_by_date(submission.date)
        winners = winners_cache[submission.date]

        correct = sum(1 for pick in submission.picks.all() if winners.get(pick.game_id) == pick.picked_team)
        submission.correct_count = correct

        entry = totals.setdefault(
            submission.instagram_id, {"correct": 0, "first_created_at": submission.created_at}
        )
        entry["correct"] += correct
        entry["first_created_at"] = min(entry["first_created_at"], submission.created_at)

    leaderboard = sorted(
        ({"instagram_id": iid, **data} for iid, data in totals.items()),
        key=lambda x: (-x["correct"], x["first_created_at"]),
    )[:3]
    for rank, entry in enumerate(leaderboard, start=1):
        entry["rank"] = rank

    return render(
        request,
        "roster/prediction_results.html",
        {"submissions": submissions, "leaderboard": leaderboard},
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


@require_GET
def stats(request):
    """방문자수(페이지뷰)/LLM 호출 수 통계. 개발자 확인용이라 /sync/와 동일한 토큰으로 보호한다."""
    expected = settings.SYNC_SECRET_TOKEN
    provided = request.GET.get("token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return HttpResponseForbidden("forbidden")

    all_stats = DailyStat.objects.all()
    totals = all_stats.aggregate(
        page_views=Sum("page_views"), unique_visitors=Sum("unique_visitors"), llm_calls=Sum("llm_calls")
    )
    daily = list(all_stats[:30])
    chart_data = [
        {
            "date": row.date.strftime("%m/%d"),
            "unique_visitors": row.unique_visitors,
            "page_views": row.page_views,
        }
        for row in reversed(daily)
    ]
    return render(
        request,
        "roster/stats.html",
        {"daily": daily, "totals": totals, "chart_data": chart_data},
    )


@require_GET
def service_worker(request):
    """Web Push 서비스워커. 스코프를 사이트 전체(/)로 주기 위해 정적 파일이 아닌 루트 경로(/sw.js)에서 서빙한다."""
    path = Path(settings.BASE_DIR) / "roster" / "static" / "roster" / "js" / "sw-source.js"
    return HttpResponse(path.read_text(), content_type="application/javascript")


@require_http_methods(["POST"])
def push_subscribe(request):
    """브라우저의 Web Push 구독 정보를 받아 '마이팀'으로 저장(갱신)한다."""
    try:
        payload = json.loads(request.body)
        team_id = payload["team_id"]
        endpoint = payload["endpoint"]
        p256dh = payload["keys"]["p256dh"]
        auth = payload["keys"]["auth"]
    except (KeyError, ValueError, TypeError):
        return HttpResponseBadRequest("invalid payload")

    team = get_object_or_404(Team, pk=team_id)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"team": team, "p256dh": p256dh, "auth": auth},
    )
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def push_unsubscribe(request):
    """구독 해제. 프론트에서 endpoint만 보내면 된다."""
    try:
        payload = json.loads(request.body)
        endpoint = payload["endpoint"]
    except (KeyError, ValueError, TypeError):
        return HttpResponseBadRequest("invalid payload")

    PushSubscription.objects.filter(endpoint=endpoint).delete()
    return JsonResponse({"ok": True})
