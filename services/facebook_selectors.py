"""Versioned, read-only DOM profiles. No browser/user input selects a config path.

Only deployment-controlled profiles are supported; tenant overrides are deliberately
not accepted from posting payloads. Diagnostics contain no DOM text or secrets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

CONFIG = Path(__file__).resolve().parents[1] / 'config' / 'facebook_selectors'
LOG = logging.getLogger(__name__)


class SelectorResolutionError(RuntimeError):
    def __init__(self, key: str, reason: str, version: str, profile: str):
        self.key, self.reason = key, reason
        super().__init__(f'{version}/{profile}/{key}: {reason}')


class SelectorRegistry:
    def __init__(self, *, version: str | None = None, profile: str | None = None,
                 locales: tuple[str, ...] | None = None):
        active = json.loads((CONFIG / 'active.json').read_text())
        self.version = version or active['version']
        self.profile = profile or active['profile']
        if not re.fullmatch(r'v[0-9]+', self.version):
            raise ValueError('Invalid selector version')
        document = json.loads((CONFIG / f'{self.version}.json').read_text())
        if document['version'] != self.version:
            raise ValueError('Selector version mismatch')
        self.rules = document['profiles'][self.profile]
        self.locales = locales or tuple(active['locales'])
        if not self.locales or any(x not in ('vi', 'en') for x in self.locales):
            raise ValueError('Unsupported selector locale')

    def locators(self, scope: Any, key: str) -> list[Any]:
        candidates = []
        for rule in self.rules[key]:
            if 'css' in rule:
                candidates.append(scope.locator(rule['css']))
                continue
            pattern = '|'.join(rule['names'][locale] for locale in self.locales).replace('/', r'\/')
            if rule.get('exact'):
                pattern = f'^(?:{pattern})$'
            candidates.append(scope.get_by_role(rule['role'], name=re.compile(pattern, re.I)))
        return candidates

    async def resolve(self, scope: Any, key: str, *, timeout: int = 2500,
                      visible: bool = True) -> Any | None:
        """Bounded fallback; ambiguity stops resolution, never picks the first match."""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            for index, locator in enumerate(self.locators(scope, key)):
                matches = []
                for target in await locator.all():
                    if (not visible or await target.is_visible()) and await target.is_enabled():
                        matches.append(target)
                if len(matches) > 1:
                    LOG.warning('selector_ambiguous version=%s profile=%s key=%s rule=%s count=%s',
                                self.version, self.profile, key, index, len(matches))
                    raise SelectorResolutionError(key, 'ambiguous', self.version, self.profile)
                if matches:
                    return matches[0]
            if asyncio.get_running_loop().time() >= deadline:
                LOG.info('selector_missing version=%s profile=%s key=%s',
                         self.version, self.profile, key)
                return None
            await asyncio.sleep(0.1)


async def active_composer_dialog(page: Any, timeout: int = 2500) -> Any | None:
    """Only one visible dialog containing an editor is a valid composer."""
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while True:
        dialogs = []
        for dialog in await page.get_by_role('dialog').all():
            if await dialog.is_visible() and await dialog.locator('[contenteditable="true"]').count():
                dialogs.append(dialog)
        if len(dialogs) > 1:
            raise SelectorResolutionError('dialog', 'ambiguous', 'v1', 'desktop')
        if dialogs:
            return dialogs[0]
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.1)
