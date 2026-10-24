"""Quản lý phiên trình duyệt: lưu storage state nguyên atomic.

Playwright giữ cookie trong bộ nhớ và chỉ ghi vào database hồ sơ khi trình
duyệt đã truy cập trang. Vì vậy việc export ``storage_state`` phải được làm
khi trình duyệt vẫn mở, và ghi ra tệp tạm rồi ``os.replace`` để tránh hỏng
file khi tiến trình bị gián đoạn.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext


async def atomic_storage_state(context: BrowserContext, destination: str | Path) -> None:
    """Lưu trạng thái trình duyệt ra tệp một cách nguyên tử.

    ``destination`` là tệp cuối cùng. Dữ liệu được ghi vào tệp tạm cùng thư
    mục rồi đổi tên, nên người đọc không bao giờ nhìn thấy tệp chưa ghi xong.
    """
    dest = Path(destination).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        await context.storage_state(path=str(temp_path))
        os.replace(temp_path, dest)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


class BrowserSessionManager:
    """Quản lý vòng đời một BrowserContext kèm theo khóa hồ sơ.

    Mỗi tài khoản Facebook chỉ được mở một hồ sơ tại một thời điểm. Khi
    đóng, khóa được giải phóng và storage state được lưu lại (nếu có).
    """

    def __init__(self, context: Any, lock: Any, destination: str | Path | None = None) -> None:
        self.context = context
        self.lock = lock
        self.destination = Path(destination).expanduser().resolve() if destination else None
        self.closed = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.destination is not None and self.context is not None:
                pages = self.context.pages
                if pages:
                    try:
                        await pages[0].goto("https://www.facebook.com/",
                                            wait_until="domcontentloaded",
                                            timeout=30000)
                    except Exception:
                        # Cookie đã được thiết lập trong bộ nhớ; trang lỗi
                        # không được làm mất chúng.
                        pass
                await atomic_storage_state(self.context, self.destination)
        finally:
            try:
                await self.context.close()
            except Exception:
                pass
            if self.lock is not None:
                try:
                    self.lock.release()
                except Exception:
                    pass
