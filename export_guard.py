"""Shared strict TDL export validation for both Bot and web publishers."""

import json
from pathlib import Path


def read_export(path, channel, first, last):
    """Validate both peer and strict bounds before either publisher sees the file."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] != channel
        or not isinstance(data.get("messages"), list)
    ):
        raise ValueError("TDL 导出的源频道或消息列表不匹配，未发送")
    rows = {}
    for row in data["messages"]:
        mid = row.get("id") if isinstance(row, dict) else None
        if type(mid) is not int or not first <= mid <= last:
            raise ValueError("TDL 导出含无效或超出范围的消息 ID，未发送")
        rows.setdefault(mid, row)
    if len(rows) != len(data["messages"]):
        data["messages"] = list(rows.values())
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return sorted(rows)


def prepare_album_range(path, channel, first, last):
    """Trim a padded --raw export, rejecting albums crossing the requested bounds."""
    read_export(path, channel, max(1, first - 10), min(2**31 - 1, last + 10))
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    selected = []
    groups = {}
    for row in data["messages"]:
        raw = row.get("raw")
        if (
            row.get("type") != "message"
            or not isinstance(raw, dict)
            or type(raw.get("ID")) is not int
            or raw["ID"] != row["id"]
            or type(raw.get("GroupedID")) is not int
        ):
            raise ValueError("缺少有效的相册元数据，未发送")
        if raw["GroupedID"]:
            groups.setdefault(raw["GroupedID"], set()).add(row["id"])
        if first <= row["id"] <= last:
            selected.append(row)
    ids = {r["id"] for r in selected}
    for members in groups.values():
        if members & ids and not members <= ids:
            raise ValueError(
                f"范围截断了相册，请包含消息 {min(members)}–{max(members)} 后重试；未发送"
            )
    # TDL preserves file order; keep groups enabled and submit oldest to newest.
    data["messages"] = [
        {"id": r["id"], "type": "message"}
        for r in sorted(selected, key=lambda r: r["id"])
    ]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return sorted(ids)
