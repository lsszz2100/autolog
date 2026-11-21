# start.sh / setup.sh 공용: 실제로 동작하는 Python을 찾아 PY 배열에 담는다.
# - Git Bash의 python/python3는 대개 MS Store 별칭(WindowsApps)이라 "Python"만 출력하고 종료하므로 실제 실행으로 검증한다.
# - run.bat과 같은 Python312를 우선한다. Python마다 Playwright(=Chromium) 버전이 다르면
#   공용 로그인 프로필(data/naver_profile)을 구버전 Chromium이 열지 못하고 바로 종료된다.
find_python() {
    local cand
    local win312="${LOCALAPPDATA:-/nonexistent}/Programs/Python/Python312/python.exe"
    if [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath &> /dev/null; then
        win312="$(cygpath -u "$LOCALAPPDATA")/Programs/Python/Python312/python.exe"
    fi
    if [ -x "$win312" ]; then
        PY=("$win312")
        return 0
    fi
    for cand in "python3" "python" "py -3"; do
        if $cand -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" &> /dev/null; then
            read -r -a PY <<< "$cand"
            return 0
        fi
    done
    return 1
}
