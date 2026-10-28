"""Validated, Pipeline-wide recurring publishing contract."""
from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CycleSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    account_ids: list[int] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)
    album_id: int | None = Field(default=None, gt=0)
    media_ids: list[int] = Field(default_factory=list)
    repeat_media: bool = True
    caption_mode: Literal['template', 'ai'] = 'template'
    template_ids: list[int] = Field(default_factory=list)
    fallback_template_id: int | None = Field(default=None, gt=0)
    unique_captions: bool = False
    topic: str = Field(default='', max_length=1000)
    posting_mode: Literal['anonymous', 'nickname'] = 'anonymous'
    timezone: str = 'Asia/Ho_Chi_Minh'
    days: list[int] = Field(default_factory=lambda: list(range(7)))
    start_time: str = '08:00'
    end_time: str = '20:00'
    cycle_interval_minutes: int = Field(default=1440, ge=1, le=525600)
    post_interval_minutes: int = Field(default=60, ge=1, le=1440)
    groups_per_cycle: int = Field(default=50, ge=1, le=10000)
    per_account_daily_limit: int = Field(default=3, ge=1)
    require_approval: bool = True

    @field_validator('account_ids', 'group_ids', 'media_ids', 'template_ids')
    @classmethod
    def ids(cls, values):
        if any(x <= 0 for x in values) or len(values) != len(set(values)):
            raise ValueError('IDs must be positive and unique')
        return values

    @field_validator('days')
    @classmethod
    def valid_days(cls, values):
        if not values or any(x not in range(7) for x in values) or len(values) != len(set(values)):
            raise ValueError('Choose distinct weekdays, Monday=0')
        return values

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError) as error:
            raise ValueError('Invalid timezone') from error
        return value

    @field_validator('start_time', 'end_time')
    @classmethod
    def valid_time(cls, value):
        if len(value) != 5 or time.fromisoformat(value).second:
            raise ValueError('Use HH:MM')
        return value

    @model_validator(mode='after')
    def content_required(self):
        if self.enabled and self.caption_mode == 'template' and not self.template_ids:
            raise ValueError('Choose an approved caption template')
        return self


class AlbumPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    media_ids: list[int] = Field(default_factory=list)


class TemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=20000)
    approved: bool = False


class GlobalStatusPayload(BaseModel):
    state: Literal['ACTIVE', 'DISABLED', 'CHECK_PENDING', 'ACCESS_ERROR', 'UNKNOWN']
    evidence: str = Field(min_length=10, max_length=1000)


class CycleRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r'^[a-zA-Z0-9_-]+$')


class PairCheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_ids: list[int] = Field(min_length=1)

    @field_validator('account_ids')
    @classmethod
    def valid_accounts(cls, values):
        if any(x <= 0 for x in values):
            raise ValueError('Account IDs must be positive')
        return sorted(set(values))
