#!/usr/bin/env python3
"""Fast-forward a clean checkout, validate it, then restart using the updated code."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def update(root=ROOT):
    root = Path(root).resolve()
    if not shutil.which("git"):
        raise RuntimeError("未找到 Git；请安装 Git 后再更新。")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise RuntimeError("请在 Robot Lab 自己的 Git 仓库中更新；下载的 ZIP 不支持 pull。")
    if git(root, "status", "--porcelain"):
        raise RuntimeError("仓库有本地改动或未跟踪文件，请先提交或移走后再更新；不会覆盖你的文件。")
    git(root, "symbolic-ref", "--quiet", "HEAD")
    git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    print("正在拉取更新……", flush=True)
    # Never stop the service until both pull and validation succeed.
    subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], check=True)
    subprocess.run([sys.executable, str(root / "scripts/preflight.py")], check=True)
    print("更新已拉取，正在重启后台服务……", flush=True)
    subprocess.run([sys.executable, str(root / "scripts/local_service.py"), "restart"], check=True)
    print("更新完成；如页面已打开，请刷新页面。", flush=True)


def main():
    try:
        update()
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        print("更新未完成。拉取或资源检查失败时不会重启服务；请检查网络、仓库权限和分支状态。",
              file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"更新未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
