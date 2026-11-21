#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
source ./pyfind.sh

# Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError로 종료되는 문제 방지
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

echo "========================================================="
echo "   AutoLog Studio 시작 중..."
echo "========================================================="

if ! find_python; then
    echo "❌ Python 3.10 이상을 찾을 수 없습니다. (Windows는 python.org 설치 시 'Add python.exe to PATH' 체크)"
    exit 1
fi
echo "🐍 Python: $("${PY[@]}" -c 'import sys; print(sys.executable)')"

# 필수 라이브러리 검사
if ! "${PY[@]}" -c "import playwright, requests, PIL, keyring, google.genai, importlib.metadata as m, sys; sys.exit(0 if tuple(map(int, m.version('playwright').split('.')[:2])) >= (1, 64) else 1)" &> /dev/null; then
    echo "⚠️ 필수 라이브러리가 없어 자동 설치를 진행합니다..."
    ./setup.sh
fi

echo "🚀 웹 대시보드 서버를 실행합니다..."
"${PY[@]}" app.py
