#!/usr/bin/env python3
"""Check the self-contained checkout before starting or restarting Robot Lab."""
import json
from pathlib import Path
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    root = Path(root)
    required = ["server.py", "board_link.py", "index.html", "app.js", "style.css",
                "assets/robot-mesh.json", "config/rig.json", "config/joints.json",
                "data/catalog.json", "data/ServoActionList.json",
                "lib/heading.js", "lib/motion.js", "lib/nv12.js", "lib/rotate.js",
                "lib/camera-window.js", "lib/robot-view.js",
                "vendor/three.module.js", "vendor/three.core.js",
                "scripts/board_agent.py", "scripts/board_bridge.py", "scripts/board_camera.py"]
    missing = [name for name in required if not (root / name).is_file() or not (root / name).stat().st_size]
    if missing:
        raise ValueError("运行文件缺失，请重新拉取完整仓库：" + ", ".join(missing))
    for script in list(root.glob("*.py")) + list((root / "scripts").glob("*.py")):
        try:
            compile(script.read_bytes(), str(script), "exec")
        except SyntaxError as exc:
            raise ValueError(f"Python 文件有语法错误：{script.name}: {exc}") from exc
    for name in ("assets/robot-mesh.json", "config/rig.json", "config/joints.json", "data/ServoActionList.json"):
        json.loads((root / name).read_text(encoding="utf-8"))
    catalog = json.loads((root / "data/catalog.json").read_text(encoding="utf-8"))
    counts = {key: len(catalog[key]) for key in ("actions", "expressions", "states")}
    if counts != {"actions": 21, "expressions": 23, "states": 16}:
        raise ValueError("动作或表情目录不完整，请检查 data/catalog.json。")
    videos = set()
    for item in catalog["expressions"] + catalog["states"]:
        for mode in ("full", "loop"):
            url = unquote(item[mode])
            name = url.removeprefix("/media/")
            if not url.startswith("/media/") or Path(name).name != name or not name.endswith(".mp4"):
                raise ValueError("表情目录包含无效媒体路径。")
            videos.add(name)
    missing = [name for name in sorted(videos) if not (root / "media" / name).is_file()
               or not (root / "media" / name).stat().st_size]
    if missing:
        raise ValueError("表情视频缺失，请重新拉取完整仓库：" + ", ".join(missing))
    return len(videos)


def main():
    try:
        count = check()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"启动检查失败：{exc}", file=sys.stderr)
        return 1
    print(f"运行资源完整（{count} 个表情视频）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
