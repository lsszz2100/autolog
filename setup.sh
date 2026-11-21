#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "========================================================="
echo "   AutoLog Studio - 환경 설정 및 설치"
echo "========================================================="

source ./pyfind.sh
if ! find_python; then
    echo "❌ Python 3.10 이상을 찾을 수 없습니다."
    exit 1
fi

echo "1. Python 라이브러리 설치 중..."
"${PY[@]}" -m pip install --upgrade --break-system-packages -r requirements.txt || "${PY[@]}" -m pip install --upgrade -r requirements.txt

echo "2. Playwright Chromium 브라우저 설치 중..."
"${PY[@]}" -m playwright install chromium

echo "3. Playwright Chromium OS 시스템 라이브러리 검사..."
"${PY[@]}" -m playwright install-deps chromium 2>/dev/null || true

echo ""
echo "🎉 모든 라이브러리와 브라우저가 정상적으로 설치되었습니다!"
echo "./start.sh 를 실행하여 프로그램을 시작하세요."
