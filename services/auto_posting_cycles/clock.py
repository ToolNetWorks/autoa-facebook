"""Timezone-aware cycle/slot arithmetic, including overnight windows."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('Timezone required')
    return moment


def allowed_at(moment, settings):
    local = moment.astimezone(ZoneInfo(settings['timezone']))
    for offset in range(-1, 15):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in settings['days']:
            continue
        start = datetime.combine(day,time.fromisoformat(settings['start_time']),tzinfo=local.tzinfo)
        end = datetime.combine(day,time.fromisoformat(settings['end_time']),tzinfo=local.tzinfo)
        if end <= start:
            end += timedelta(days=1)
        candidate = max(local,start)
        if candidate < end:
            return candidate.astimezone(timezone.utc)
    raise ValueError('No allowed posting window')


def day_bounds(moment, settings):
    local = moment.astimezone(ZoneInfo(settings['timezone']))
    start = datetime.combine(local.date(),time.min,tzinfo=local.tzinfo)
    return start.astimezone(timezone.utc).isoformat(), (start+timedelta(days=1)).astimezone(timezone.utc).isoformat()
