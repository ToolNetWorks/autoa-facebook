from datetime import datetime,timezone
import pytest
from pydantic import ValidationError
from schemas.auto_cycles import CycleSettings
from services.auto_posting_cycles.clock import allowed_at


@pytest.mark.parametrize('moment,expected',[
    ('2026-01-05T00:00:00+00:00','2026-01-05T01:00:00+00:00'),
    ('2026-01-05T02:00:00+00:00','2026-01-05T02:00:00+00:00'),
    ('2026-01-05T14:00:00+00:00','2026-01-12T01:00:00+00:00'),
])
def test_weekday_timezone_boundaries(moment,expected):
    settings=CycleSettings(days=[0],timezone='Asia/Ho_Chi_Minh').model_dump()
    assert allowed_at(datetime.fromisoformat(moment),settings).isoformat()==expected


def test_overnight_uses_the_start_days_permission():
    settings=CycleSettings(days=[6],timezone='Asia/Ho_Chi_Minh',start_time='22:00',end_time='02:00').model_dump()
    monday=datetime(2026,1,4,18,tzinfo=timezone.utc) # Monday 01:00 in Vietnam, Sunday's window.
    assert allowed_at(monday,settings)==monday


def test_invalid_timezone_and_empty_days_fail_at_boundary():
    with pytest.raises(ValidationError):CycleSettings(timezone='Invalid/Zone')
    with pytest.raises(ValidationError):CycleSettings(days=[])
