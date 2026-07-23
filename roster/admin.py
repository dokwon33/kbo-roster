from django.contrib import admin

from .models import Player, RosterEvent, Team


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name")
    search_fields = ("name", "short_name")


class RosterEventInline(admin.TabularInline):
    model = RosterEvent
    extra = 0
    fields = ("event_date", "event_type", "team", "reason", "source", "source_name", "source_url")
    ordering = ("-event_date",)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("name", "team", "position", "back_number", "kbo_player_id", "current_status_display")
    list_filter = ("team", "position")
    search_fields = ("name", "kbo_player_id")
    fields = ("name", "team", "position", "back_number", "birth_date", "kbo_player_id", "photo_url")
    inlines = [RosterEventInline]

    @admin.display(description="현재 상태")
    def current_status_display(self, obj):
        event = obj.current_status
        return event.get_event_type_display() if event else "-"


@admin.register(RosterEvent)
class RosterEventAdmin(admin.ModelAdmin):
    list_display = ("event_date", "player", "team", "event_type", "reason", "source", "source_name")
    list_filter = ("event_type", "source", "team")
    search_fields = ("player__name", "reason", "note", "source_name")
    date_hierarchy = "event_date"
    autocomplete_fields = ("player", "team")


admin.site.site_header = "KBO 로스터 트래커"
admin.site.site_title = "KBO 로스터 트래커"
