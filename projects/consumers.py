from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import ProcessingJob


class JobStatusConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope['user']
        job_id = self.scope['url_route']['kwargs']['job_id']

        if not user.is_authenticated:
            await self.close(code=4401)
            return

        job_data = await self.get_owned_job(job_id, user.id)
        if job_data is None:
            await self.close(code=4404)
            return

        self.group_name = f'job_{job_id}'
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json(job_data)

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def job_status(self, event):
        await self.send_json(event['data'])

    @database_sync_to_async
    def get_owned_job(self, job_id, user_id):
        try:
            job = ProcessingJob.objects.get(id=job_id, project__owner_id=user_id)
        except (ProcessingJob.DoesNotExist, ValueError):
            return None

        return {
            'id': str(job.id),
            'project': str(job.project_id),
            'job_type': job.job_type,
            'status': job.status,
            'progress': job.progress,
            'message': job.message,
            'parameters': job.parameters,
            'error': (
                'Processing failed. Please try again or contact support.'
                if job.status == ProcessingJob.FAILED
                else ''
            ),
            'output_asset': str(job.output_asset_id) if job.output_asset_id else None,
            'updated_at': job.updated_at.isoformat(),
        }
