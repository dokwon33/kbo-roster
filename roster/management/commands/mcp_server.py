from django.core.management.base import BaseCommand

from roster.mcp_server import mcp


class Command(BaseCommand):
    help = "KBO 로스터 데이터를 읽기 전용 도구로 노출하는 MCP 서버를 stdio로 실행합니다."

    def handle(self, *args, **options):
        mcp.run(transport="stdio")
