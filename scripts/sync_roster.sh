#!/bin/bash
# 매일 KBO 공식 사이트에서 1군 등록/말소 현황을 가져와 DB에 반영한다.
# crontab: 0 9 * * * /Users/ldk/kbo-roster/scripts/sync_roster.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

source venv/bin/activate
pip install -q -r requirements.txt
python manage.py sync_roster >> logs/sync_roster.log 2>&1
