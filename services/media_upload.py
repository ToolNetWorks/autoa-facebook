"""Attach exactly one image and wait for actual composer preview readiness."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence
from services.facebook_selectors import SelectorRegistry, active_composer_dialog


async def attachment_ready(dialog: Any) -> bool:
    registry = SelectorRegistry()
    previews = registry.locators(dialog, 'preview')[0]
    count = 0
    for image in await previews.all():
        if await image.is_visible() and await image.evaluate('(img) => img.complete && img.naturalWidth > 0'):
            count += 1
    # Uploaded previews can become CDN URLs; a visible Remove photo control
    # is also explicit evidence of an attachment, unlike a profile avatar.
    removals = registry.locators(dialog, 'remove_photo')[0]
    remove_count = sum([await item.is_visible() for item in await removals.all()])
    progress = dialog.locator('[role="progressbar"], [aria-busy="true"]')
    busy = any([await item.is_visible() for item in await progress.all()])
    return max(count, remove_count) == 1 and not busy


async def upload_media(page: Any, media_paths: Sequence[str | Path],
                       dialog_selector: str = "div[role='dialog']", *,
                       dialog: Any = None, timeout: int = 15000) -> bool:
    paths = [str(p) for p in media_paths if p]
    if len(paths) != 1 or not Path(paths[0]).is_file():
        return False
    dialog = dialog if dialog is not None else await active_composer_dialog(page)
    if dialog is None:
        return False
    if await attachment_ready(dialog):
        return False  # refuse to add to an existing attachment
    registry = SelectorRegistry()
    file_input = await registry.resolve(dialog, 'file_input', visible=False, timeout=200)
    if file_input is not None:
        # Playwright supports hidden inputs; visibility is not required here.
        await file_input.set_input_files(paths, timeout=timeout)
    else:
        button = await registry.resolve(dialog, 'media', timeout=1000)
        if button is None:
            return False
        async with page.expect_file_chooser(timeout=timeout) as pending:
            await button.click(timeout=5000)
        chooser = await pending.value
        await chooser.set_files(paths, timeout=timeout)
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while True:
        if await attachment_ready(dialog):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)
