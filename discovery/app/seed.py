"""Synthetic Catalogue messages, until Catalogue's publisher is available."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import uuid
from messaging.envelope import Envelope


def demo_messages(now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo('Europe/Dublin')
    day = now.astimezone(zone).date()
    friday = day + timedelta(days=(4 - day.weekday()) % 7)
    if datetime.combine(friday, time(20), zone) <= now:
        friday += timedelta(days=7)
    specs = [
        ('demo-jazz', 'Quiet jazz by candlelight', 'Calm acoustic music in a small room', ['music', 'calm', 'jazz'], 0, 20),
        ('demo-rock', 'Loud rock show', 'Electric guitars and energetic music', ['music', 'rock'], 0, 21),
        ('demo-morning', 'Morning piano', 'Calm piano music over breakfast', ['music', 'calm'], 0, 10),
        ('demo-saturday', 'Saturday acoustic session', 'Calm music from local performers', ['music', 'calm'], 1, 20),
    ]
    return [Envelope(message_id=str(uuid.uuid4()), type='event.changed', version=1,
                     occurred_at=now.isoformat(), producer='discovery-demo-seeder', payload={
                         'event_id': event_id, 'event_version': 1, 'action': 'created',
                         'title': title, 'description': description, 'tags': tags,
                         'starts_at': datetime.combine(friday + timedelta(days=offset), time(hour), zone).isoformat()
                     }) for event_id, title, description, tags, offset, hour in specs]
