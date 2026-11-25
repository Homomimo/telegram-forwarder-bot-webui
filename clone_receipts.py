"""Strict receipts from the bundled TDL extension. Never infer target IDs from history."""
import json
import secrets
import tempfile
from pathlib import Path

import accounts
class Uncertain(Exception):
    """A publication may already have reached Telegram."""


class Delivered(dict):
    engine = "tdl"

    def __init__(self, mapping, actor):
        super().__init__(mapping)
        self.actor = str(actor)


def read_receipts(path, token, channel, dest, members, target=None, actor=None):
    try:
        path = Path(path)
        if not 0 < path.stat().st_size <= 65536:
            raise ValueError
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        result = {}
        sender = None
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("version")) is not int
                or row["version"] != 1
                or row.get("token") != token
                or any(type(row.get(k)) is not int for k in ("from", "to", "source", "target", "actor"))
                or row["from"] != int(channel)
                or row["to"] != int(dest)
                or row["source"] not in members
                or not 0 < row["target"] < 2**31
                or row["actor"] <= 0
                or row["source"] in result
                or row["target"] in result.values()
                or row.get("operation") != ("edit" if target is not None else "send")
                or (target is not None and row["target"] != int(target))
                or (actor and row["actor"] != int(actor))
                or (sender is not None and sender != row["actor"])
            ):
                raise ValueError
            result[row["source"]] = row["target"]
            sender = row["actor"]
        if set(result) != set(members) or not result:
            raise ValueError
        return Delivered(result, sender)
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise Uncertain("TDL 回执不完整或不匹配；请核对目标频道，勿直接重发") from e


def prepare_clone(data, binary, job, request):
    if not accounts.summary(data)["tdl"]:
        raise ValueError("TDL 未配置，请先在账号设置中扫码登录后重试")
    root = Path(data) / "receipts"
    root.mkdir(mode=0o700, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f"{job['id']}-", dir=root))
    token = secrets.token_hex(24)
    receipt = attempt / "result.jsonl"
    target = request.target
    args = [
        binary, "forward", "--from", f"https://t.me/c/{request.channel}/{request.ids[0]}",
        "--to", job["dest"], "--mode", "clone",
    ]
    if target is not None or not request.grouped:
        args.append("--single")
    args += ["--relay-receipt", str(receipt), "--relay-token", token,
             "--relay-members", ",".join(map(str, request.ids))]
    if target is not None:
        args += ["--relay-edit-target", str(target)]
        if request.actor:
            args += ["--relay-actor", str(request.actor)]
    (attempt / "request.json").write_text(json.dumps({
        "job": job["id"], "token": token, "channel": request.channel, "dest": job["dest"],
        "members": request.ids, "target": target, "actor": request.actor,
    }), encoding="utf-8")
    return args, receipt, token
