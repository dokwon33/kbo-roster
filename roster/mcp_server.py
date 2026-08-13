"""KBO 로스터 데이터를 노출하는 읽기 전용 MCP 서버.

Claude Desktop 같은 MCP 클라이언트가 `python manage.py mcp_server`로 이 서버에
stdio로 접속해, 자연어 질의("한화 이글스 1군 로스터 보여줘")를 아래 도구 호출로
변환해 실행한다. 새 도구를 추가할 때는 이 파일에 `@mcp.tool()` + `@sync_to_async`
함수만 더하면 된다 (management 커맨드는 이 모듈의 `mcp` 인스턴스를 그대로 실행할
뿐 변경할 필요 없음).

FastMCP는 도구를 자체 asyncio 이벤트 루프 안에서 호출하는데, Django ORM은 동기
호출을 async 컨텍스트에서 그대로 실행하는 것을 막는다(`SynchronousOnlyOperation`).
그래서 각 도구 함수는 `@sync_to_async`로 감싸 별도 스레드에서 실행한다 —
`@mcp.tool()`은 이 래핑 이후 남은 `__call__`이 코루틴 함수인 것을 보고 자동으로
awaited 코루틴 도구로 등록한다.
"""

from dataclasses import asdict
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.utils import timezone
from mcp.server.fastmcp import FastMCP

from .models import Player, RosterEvent, Team
from .scraping import fetch_attendance_rows, fetch_standings_1gun, fetch_standings_2gun
from .views import ROSTER_STALE_DAYS, _attach_current_status, _cached_fetch, _drop_stale

mcp = FastMCP("kbo-roster")


def _find_team(team_name):
    return Team.objects.filter(name=team_name).first() or Team.objects.filter(name__icontains=team_name).first()


def _find_player(player_name):
    return Player.objects.filter(name=player_name).first() or Player.objects.filter(name__icontains=player_name).first()


@mcp.tool()
@sync_to_async
def list_teams() -> list[str]:
    """등록된 KBO 구단 이름 목록을 반환한다."""
    return list(Team.objects.values_list("name", flat=True))


@mcp.tool()
@sync_to_async
def get_team_roster(team_name: str) -> dict:
    """특정 구단의 현재 1군 등록 선수와 2군/부상 등 기타 상태 선수 명단을 반환한다."""
    team = _find_team(team_name)
    if team is None:
        return {"error": f"'{team_name}' 이름과 일치하는 구단을 찾을 수 없습니다."}

    players = _drop_stale(_attach_current_status(team.players.all()))
    active = [p for p in players if p.current_status and p.current_status.event_type == RosterEvent.ACTIVE_1GUN]
    others = [p for p in players if p not in active]

    def _row(p):
        return {
            "name": p.name,
            "position": p.get_position_display() if p.position else "",
            "back_number": p.back_number,
            "status": p.current_status.get_event_type_display() if p.current_status else "",
            "status_since": p.current_status.event_date.isoformat() if p.current_status else None,
        }

    return {
        "team": team.name,
        "active_1gun": [_row(p) for p in active],
        "others": [_row(p) for p in others],
    }


@mcp.tool()
@sync_to_async
def get_player_status(player_name: str) -> dict:
    """선수의 현재 상태와 등록/말소 등 전체 이력을 반환한다."""
    player = _find_player(player_name)
    if player is None:
        return {"error": f"'{player_name}' 이름과 일치하는 선수를 찾을 수 없습니다."}

    events = player.events.select_related("team").all()
    return {
        "name": player.name,
        "team": player.team.name if player.team else None,
        "position": player.get_position_display() if player.position else "",
        "back_number": player.back_number,
        "current_status": player.current_status.get_event_type_display() if player.current_status else None,
        "history": [
            {
                "date": e.event_date.isoformat(),
                "event_type": e.get_event_type_display(),
                "team": e.team.name if e.team else None,
                "reason": e.reason,
                "source": e.get_source_display(),
                "source_name": e.source_name,
                "source_url": e.source_url,
            }
            for e in events
        ],
    }


@mcp.tool()
@sync_to_async
def get_standings() -> dict:
    """1군 전체 순위와 2군(퓨처스) 북부/남부 순위를 반환한다."""
    today = timezone.localdate()
    standings_1gun = _cached_fetch(f"standings_1gun_{today}", fetch_standings_1gun)
    rows_2gun = _cached_fetch(f"standings_2gun_{today}", fetch_standings_2gun)

    return {
        "1군": [asdict(r) for r in standings_1gun],
        "2군_북부": [asdict(r) for r in rows_2gun if r.division == "북부"],
        "2군_남부": [asdict(r) for r in rows_2gun if r.division == "남부"],
    }


@mcp.tool()
@sync_to_async
def get_recent_roster_events(days: int = 30, team_name: str = "") -> dict:
    """최근 며칠(days) 이내의 등록/말소 등 로스터 변동 이력을 최신순으로 반환한다.

    team_name을 지정하면 해당 구단 관련 이벤트만 반환한다(빈 문자열이면 전체 구단).
    """
    days = min(days, ROSTER_STALE_DAYS * 3)
    cutoff = timezone.localdate() - timedelta(days=days)
    events = RosterEvent.objects.select_related("player", "team").filter(event_date__gte=cutoff)

    if team_name:
        team = _find_team(team_name)
        if team is None:
            return {"error": f"'{team_name}' 이름과 일치하는 구단을 찾을 수 없습니다."}
        events = events.filter(team=team)

    return {
        "events": [
            {
                "date": e.event_date.isoformat(),
                "player": e.player.name,
                "team": e.team.name if e.team else None,
                "event_type": e.get_event_type_display(),
                "reason": e.reason,
            }
            for e in events[:100]
        ]
    }


@mcp.tool()
@sync_to_async
def get_attendance_stats() -> list[dict]:
    """구단별 평균 좌석 매진율, 요일별/상대구단별 매진율 통계를 반환한다."""
    rows = _cached_fetch("attendance_rows", fetch_attendance_rows)
    home_rows = [r for r in rows if r.capacity]

    team_stats = []
    for team in sorted({r.home for r in home_rows}):
        team_rows = [r for r in home_rows if r.home == team]
        team_stats.append(
            {
                "team": team,
                "stadium": team_rows[0].stadium,
                "total_games": len(team_rows),
                "avg_sellout_rate": round(100 * sum(r.sellout_rate for r in team_rows) / len(team_rows), 1),
            }
        )

    team_stats.sort(key=lambda x: -x["avg_sellout_rate"])
    return team_stats
