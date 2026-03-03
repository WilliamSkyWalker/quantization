"""WebSocket consumers for task progress and Polymarket push."""
import json

from channels.generic.websocket import AsyncWebsocketConsumer


class TaskConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.channel_layer.group_add('tasks', self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard('tasks', self.channel_name)

    async def task_update(self, event):
        """Receive task update from channel layer and send to WebSocket."""
        await self.send(text_data=json.dumps(event['data'], ensure_ascii=False))


class PolymarketConsumer(AsyncWebsocketConsumer):
    """Polymarket 实时赔率 + 告警 WebSocket 推送。"""

    async def connect(self):
        await self.channel_layer.group_add('polymarket', self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard('polymarket', self.channel_name)

    async def price_update(self, event):
        """转发实时赔率更新。"""
        await self.send(text_data=json.dumps({
            "type": "price_update",
            "data": event["data"],
        }, ensure_ascii=False))

    async def alert(self, event):
        """转发新告警。"""
        await self.send(text_data=json.dumps({
            "type": "alert",
            "data": event["data"],
        }, ensure_ascii=False))
