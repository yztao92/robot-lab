#!/usr/bin/env python3
"""Extract the current presentation's metadata; never reuse its old body videos."""
import hashlib
import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import quote


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root.parent / "表情&动作",
                        help="维护者的原始素材目录，含 展示.html、ServoActionList.json 和 表情/")
    assets = parser.parse_args().source.resolve()
    source = assets / "展示.html"
    html = source.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()

    def literal(name):
        start = html.index("const " + name + "=") + len("const " + name + "=")
        return decoder.raw_decode(html, start)[0]

    items, motion = literal("items"), literal("motionData")

    def media(path):
        file = assets / path
        if not file.is_file() or file.parent.name != "表情" or file.suffix != ".mp4":
            raise ValueError(f"Invalid/missing expression video: {path}")
        return "/media/" + quote(file.name)

    def face(item):
        full = media(item["screenFile"])
        return {
            "id": item["id"], "name": item["name"],
            "family": item["family"], "description": item["description"],
            "full": full,
            "loop": media(item["alternateFile"]) if item.get("alternateFile") else full,
            "action_id": item.get("actionId"),
        }

    catalog = {
        "schema_version": 1,
        "source": "表情&动作/展示.html",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "angle_convention": "robot-self; positive: pitch down, roll left, yaw left, arm forward/outward",
        "actions": [dict(action, protocol_id=int(action["id"][1:]) - 1)
                    for action in motion["actions"]],
        "expressions": [face(item) for item in items if item["group"] == "表情"],
        "states": [face(item) for item in items if item["group"] == "声波"],
    }
    counts = {key: len(catalog[key]) for key in ("actions", "expressions", "states")}
    if counts != {"actions": 21, "expressions": 23, "states": 16}:
        raise ValueError(f"Source changed; review catalog counts: {counts}")
    output = root / "data" / "catalog.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Ship exact originals with the checkout; end users need no sibling source directory.
    shutil.copy2(assets / "ServoActionList.json", output.parent / "ServoActionList.json")
    target = root / "media"
    target.mkdir(exist_ok=True)
    for file in (assets / "表情").glob("*.mp4"):
        shutil.copy2(file, target / file.name)
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
