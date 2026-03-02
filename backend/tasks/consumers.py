"""WebSocket consumer for task progress push."""
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
