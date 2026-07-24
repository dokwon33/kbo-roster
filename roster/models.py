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
