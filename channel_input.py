"""Parse channel settings without making requests or accepting invite links."""

import re
from dataclasses import dataclass

from telethon import types, utils

MAX_CHANNEL_ID = 997_852_516_352
USERNAME = r"[a-zA-Z][a-zA-Z0-9_]{0,31}"
LINK = re.compile(
    rf"https://t\.me/(?:c/(?P<channel>[1-9][0-9]{{0,11}})|(?P<username>{USERNAME}))"
    r"(?:/(?P<post>[1-9][0-9]{0,9}))?/?(?:\?single)?",
    re.ASCII | re.IGNORECASE,
)
# Telegram actions are not public channel usernames.
RESERVED = frozenset(
    "c s joinchat addlist share proxy socks login iv addstickers addemoji setlanguage confirmphone boost giftcode invoice bg m me self".split()
)


def numeric_channel(text: str) -> int | None:
    value = text.strip()
    if not re.fullmatch(r"-?[0-9]{1,13}", value):
        return None
    cid = int(value)
    if value.startswith("-"):
        cid, kind = utils.resolve_id(cid)
        if kind is not types.PeerChannel:
            return None
    return cid if 0 < cid <= MAX_CHANNEL_ID else None


@dataclass(frozen=True)
class ChannelReference:
    peer: int | str  # bare ID, or a public username requiring a Telegram lookup
    post: int | None = None


def parse_reference(text: str) -> ChannelReference:
    if not isinstance(text, str) or len(text) > 256:
        raise ValueError("频道输入无效")
    value = text.strip()
    cid = numeric_channel(value)
    if cid is not None:
        return ChannelReference(cid)
    if value.lower().startswith(
        ("https://t.me/+", "https://t.me/joinchat/", "tg://join")
    ):
        raise ValueError(
            "邀请链接不能确定频道 ID，不会自动加入。请粘贴频道内的一条原帖链接。"
        )
    post = None
    if value.startswith("@") and re.fullmatch(USERNAME, value[1:], re.ASCII):
        username = value[1:]
    elif match := LINK.fullmatch(value):
        if match["post"]:
            post = int(match["post"])
            if post >= 2**31:
                raise ValueError("帖子 ID 超出范围")
        if match["channel"]:
            cid = numeric_channel(match["channel"])
            if cid is None:
                raise ValueError("频道 ID 超出范围")
            return ChannelReference(cid, post)
        username = match["username"]
    else:
        raise ValueError(
            "频道输入无效，请使用裸 ID、Telegram 标记 ID、@用户名或 t.me 频道／原帖链接。"
        )
    if username.lower() in RESERVED:
        raise ValueError("这不是频道地址，请粘贴频道内的原帖链接。")
    return ChannelReference(username.lower(), post)
