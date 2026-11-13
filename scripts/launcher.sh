# Shared by the macOS double-click entry points. Run from the checkout root.
robotlab_fail() {
    print -u2 -- "$1"
    if [[ -t 0 ]]; then
        read -r "robotlab_reply?按回车关闭窗口……"
    fi
    return 1
}

robotlab_run() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        robotlab_fail "双击启动仅支持 macOS。其他系统的纯预览请运行 python3 server.py --no-board。"
        return 1
    fi
    local robotlab_python="" robotlab_candidate
    for robotlab_candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
        if command -v "$robotlab_candidate" >/dev/null 2>&1 && \
           "$robotlab_candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            robotlab_python="$(command -v "$robotlab_candidate")"
            break
        fi
    done
    if [[ -z "$robotlab_python" ]]; then
        robotlab_fail "需要 Python 3.10 或更新版本。请先安装：https://www.python.org/downloads/macos/"
        return 1
    fi
    export ROBOTLAB_PYTHON="$robotlab_python"
    local robotlab_result=0
    if [[ "$1" == "update" ]]; then
        "$robotlab_python" scripts/update.py || robotlab_result=$?
    else
        "$robotlab_python" scripts/local_service.py "$1" || robotlab_result=$?
    fi
    if (( robotlab_result != 0 )); then
        robotlab_fail "操作失败，请查看上面的提示。"
        return "$robotlab_result"
    fi
    if [[ "$1" != "stop" ]]; then
        open http://127.0.0.1:8769/
    fi
}
