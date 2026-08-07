from django.db import models
from django.urls import reverse
from django.utils.functional import cached_property


class Team(models.Model):
    name = models.CharField(max_length=20, unique=True)
    short_name = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Player(models.Model):
    POSITION_CHOICES = [
        ("투", "투수"),
        ("포", "포수"),
        ("내", "내야수"),
        ("외", "외야수"),
    ]

    name = models.CharField(max_length=50)
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="players")
    position = models.CharField(max_length=10, choices=POSITION_CHOICES, blank=True)
    back_number = models.CharField(max_length=5, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    kbo_player_id = models.CharField(
        max_length=20, blank=True, unique=True, null=True,
        help_text="KBO 공식 사이트 선수 고유 코드 (pcode). 사진 URL 조회 등에 사용",
    )
    photo_url = models.URLField(blank=True, help_text="KBO 공식 사이트 선수 사진 URL (재호스팅하지 않고 링크만 저장)")
    news_summary = models.TextField(blank=True, help_text="네이버 뉴스 기사 기반 AI 요약 캐시")
    news_articles_cache = models.JSONField(
        default=list, blank=True, help_text="요약 생성에 사용된 기사 목록(제목/설명/링크/날짜) 캐시"
    )
    news_summary_updated_at = models.DateTimeField(
        null=True, blank=True, help_text="AI 요약을 마지막으로 새로 생성한 시각"
    )

    class Meta:
        ordering = ["team__name", "name"]
        constraints = [
            # KBO 사이트는 선수 고유 ID를 이 페이지에서 제공하지 않아 이름 기준으로 식별한다.
            # 동명이인이 있을 경우 관리자 화면에서 수동으로 구분해야 한다.
            models.UniqueConstraint(fields=["name"], name="unique_player_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.team})" if self.team else self.name

    def get_absolute_url(self):
        return reverse("roster:player_detail", args=[self.pk])

    @cached_property
    def current_status(self):
        return self.events.order_by("-event_date", "-id").first()


class RosterEvent(models.Model):
    ACTIVE_1GUN = "ACTIVE_1GUN"
    OPTIONED_2GUN = "OPTIONED_2GUN"
    INJURED = "INJURED"
    MILITARY = "MILITARY"
    SUSPENDED = "SUSPENDED"
    RELEASED = "RELEASED"
    OTHER = "OTHER"

    EVENT_TYPE_CHOICES = [
        (ACTIVE_1GUN, "1군 등록"),
        (OPTIONED_2GUN, "2군 말소"),
        (INJURED, "부상자 명단"),
        (MILITARY, "군 입대/전역"),
        (SUSPENDED, "출장 정지"),
        (RELEASED, "방출/은퇴"),
        (OTHER, "기타"),
    ]

    SOURCE_SCRAPER = "SCRAPER"
    SOURCE_MANUAL = "MANUAL"
    SOURCE_MEDIA = "MEDIA"
    SOURCE_CHOICES = [
        (SOURCE_SCRAPER, "KBO 공식 자동 수집"),
        (SOURCE_MANUAL, "수동 입력"),
        (SOURCE_MEDIA, "미디어/SNS 수집"),
    ]

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="events")
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    event_date = models.DateField()
    reason = models.CharField(max_length=200, blank=True, help_text="부상 부위, 사유 등 (KBO 공식 페이지에는 표기되지 않아 수동 또는 미디어 수집으로 기재)")
    note = models.TextField(blank=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_name = models.CharField(
        max_length=100, blank=True, help_text="사유 출처 (예: 한화 이글스 공식 SNS, 연합뉴스 등)"
    )
    source_url = models.URLField(blank=True, help_text="사유 출처 원문 링크")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-event_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["player", "event_date", "event_type"],
                name="unique_event_per_player_per_day",
            ),
        ]

    def __str__(self):
        return f"{self.event_date} {self.player.name} - {self.get_event_type_display()}"


class Feedback(models.Model):
    content = models.TextField()
    contact = models.CharField(max_length=200, blank=True, help_text="답변 받을 이메일 등 (선택)")
    created_at = models.DateTimeField(auto_now_add=True)
    email_sent = models.BooleanField(default=False, help_text="개발자 이메일로 발송 성공 여부")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} 의견"


class PushSubscription(models.Model):
    """브라우저 Web Push 구독 정보. 사용자가 '마이팀'으로 고른 팀의 로스터 변동을 알림으로 받는다."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(unique=True, max_length=500)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.team.name} 구독 ({self.endpoint[:40]}...)"


class DailyStat(models.Model):
    """방문자수(페이지뷰)와 LLM(Groq) 호출 수를 날짜별로 집계한다.

    요청마다 행을 쌓지 않고 날짜당 한 행에 카운트만 누적해, 트래픽이 늘어도
    테이블이 무한정 커지지 않게 한다.
    """

    date = models.DateField(unique=True)
    page_views = models.PositiveIntegerField(default=0)
    unique_visitors = models.PositiveIntegerField(default=0)
    llm_calls = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} 방문 {self.page_views}(순 {self.unique_visitors}) / LLM {self.llm_calls}"


class PredictionSubmission(models.Model):
    """승부예측 이벤트의 하루치 응모 1건. 세션과 인스타그램 아이디 양쪽으로 중복 참여를 막는다."""

    date = models.DateField()
    instagram_id = models.CharField(max_length=50)
    session_key = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["date", "instagram_id"], name="unique_instagram_per_day"),
            models.UniqueConstraint(fields=["date", "session_key"], name="unique_session_per_day"),
        ]

    def __str__(self):
        return f"{self.date} {self.instagram_id}"


class PredictionPick(models.Model):
    """응모 1건에 포함된 개별 경기 예측 (원정/홈 중 승리 예상 팀)."""

    submission = models.ForeignKey(PredictionSubmission, on_delete=models.CASCADE, related_name="picks")
    game_id = models.CharField(max_length=20, help_text="KBO 경기 고유 ID (G_ID)")
    away_team = models.CharField(max_length=20)
    home_team = models.CharField(max_length=20)
    picked_team = models.CharField(max_length=20, help_text="away_team 또는 home_team 값 중 하나")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "game_id"], name="unique_pick_per_game"),
        ]

    def __str__(self):
        return f"{self.away_team} vs {self.home_team} → {self.picked_team}"
