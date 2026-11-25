"""Offline Telegram boundary. Demo never connects a Telegram client or runs TDL."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from telethon import errors, types
from clone_receipts import Delivered


class DemoTransport:
    source = 9876543210
    groups = {41: 70, 42: 70, 43: 70, 45: 80, 46: 80}

    def __init__(self, queue):
        self.queue = queue

    @staticmethod
    def public_channel(username):
        fixtures = {
            "demo_source": (9876543210, "演示素材"),
            "demo_archive": (1234567890, "演示归档"),
            "demo_news": (2345678901, "演示快讯"),
        }
        if username not in fixtures:
            raise ValueError("离线 Demo 不查询 Telegram。请试 @demo_news，或填写数字 ID、私有频道链接。")
        return fixtures[username]

    def receipt(self, mid):
        return 1000 + 100 * self.queue.current_task.job_id + mid

    def export(self, source, low, high):
        if str(source).startswith("@"):
            source, _ = self.public_channel(source[1:].lower())
        if str(source) != str(self.source):
            raise ValueError("演示仅提供源频道 9876543210 的 40–47 号帖子")
        return {
            "id": self.source,
            "messages": [
                {
                    "type": "message",
                    "id": mid,
                    "raw": {"ID": mid, "GroupedID": self.groups.get(mid, 0)},
                }
                for mid in range(40, 48)
                if low <= mid <= high
            ],
        }

    async def get_input_entity(self, peer):
        return types.InputPeerChannel(peer.channel_id, 123)

    async def get_entity(self, peer):
        return types.Channel(
            id=peer.channel_id,
            title="Demo",
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc),
            creator=True,
            broadcast=True,
        )

    async def get_messages(self, peer, *, ids):
        return types.Message(
            id=ids,
            peer_id=types.PeerChannel(peer.channel_id),
            message="演示",
            grouped_id=self.groups.get(ids),
        )

    async def __call__(self, request):
        await asyncio.sleep(0.2)
        if any(mid in (45, 46) for mid in request.id):
            raise errors.ChatForwardsRestrictedError(request)
        if 47 in request.id:
            raise TimeoutError("模拟发送后回执丢失，请核对，不自动重发")
        return SimpleNamespace(
            updates=[
                types.UpdateMessageID(id=self.receipt(mid), random_id=rid)
                for mid, rid in zip(request.id, request.random_id)
            ]
        )

    async def clone(self, ids):
        await asyncio.sleep(0.2)
        return Delivered({mid: self.receipt(mid) for mid in ids}, 42)

    async def disconnect(self):
        pass
