"""Versioned, read-only DOM profiles. No browser/user input selects a config path.

Only deployment-controlled profiles are supported; tenant overrides are deliberately
not accepted from posting payloads. Diagnostics contain no DOM text or secrets.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
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
        self.notices = document.get('notices', {})
        self._validate()
        self.locales = locales or tuple(active['locales'])
        if not self.locales or any(x not in ('vi', 'en') for x in self.locales):
            raise ValueError('Unsupported selector locale')

    def _validate(self) -> None:
        required = {'composer', 'editor', 'publish', 'file_input', 'preview'}
        if not isinstance(self.rules, dict) or not required.issubset(self.rules):
            raise ValueError('Incomplete selector profile')
        for key, rules in self.rules.items():
            if not rules or len(rules) > 8:
                raise ValueError(f'Invalid selector alternatives: {key}')
            for rule in rules:
                if sum(name in rule for name in ('css', 'role', 'text')) != 1:
                    raise ValueError(f'Invalid selector rule: {key}')
                if 'css' in rule:
                    if not isinstance(rule['css'], str) or not rule['css'].strip() or 'xpath=' in rule['css'] or rule['css'].startswith('/'):
                        raise ValueError(f'Invalid CSS rule: {key}')
                elif set(rule.get('names', {})) != {'vi', 'en'}:
                    raise ValueError(f'Missing locale: {key}')
                else:
                    for pattern in rule['names'].values():
                        re.compile(pattern)

    def css(self, key: str) -> str:
        rules = self.rules[key]
        if len(rules) != 1 or 'css' not in rules[0]:
            raise ValueError(f'Not a CSS rule: {key}')
        return rules[0]['css']

    def pattern(self, key: str) -> re.Pattern:
        rule = self.rules[key][0]
        pattern = '|'.join(rule['names'][locale] for locale in self.locales)
        return re.compile(f'^(?:{pattern})$' if rule.get('exact') else pattern, re.I)

    def text(self, scope: Any, value: str) -> Any:
        return scope.get_by_text(value, exact=True)

    def all(self, scope: Any, key: str) -> Any:
        candidates = self.locators(scope, key)
        result = candidates[0]
        for candidate in candidates[1:]:
            result = result.or_(candidate)
        return result

    def locators(self, scope: Any, key: str) -> list[Any]:
        candidates = []
        for rule in self.rules[key]:
            if 'css' in rule:
                candidates.append(scope.locator(rule['css']))
                continue
            pattern = '|'.join(rule['names'][locale] for locale in self.locales).replace('/', r'\/')
            if rule.get('exact'):
                pattern = f'^(?:{pattern})$'
            expression = re.compile(pattern, re.I)
            candidates.append(scope.get_by_text(expression) if rule.get('text') else
                              scope.get_by_role(rule['role'], name=expression))
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
    registry = current_registry()
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    while True:
        dialogs = []
        for dialog in await registry.all(page, 'dialogs').all():
            if await dialog.is_visible() and await registry.all(dialog, 'editable').count():
                dialogs.append(dialog)
        if len(dialogs) > 1:
            raise SelectorResolutionError('dialog', 'ambiguous', registry.version, registry.profile)
        if dialogs:
            return dialogs[0]
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.1)


_PINNED: ContextVar[SelectorRegistry | None] = ContextVar('facebook_selectors', default=None)


def current_registry() -> SelectorRegistry:
    return _PINNED.get() or SelectorRegistry()


def selector_workflow(function):
    """Pin a private snapshot for this task and every child coroutine until exit.

    Deployment may switch active.json for the NEXT job; neither a job payload nor
    another tenant can choose a path/profile or mutate this job's rules.
    """
    @wraps(function)
    async def run(*args, **kwargs):
        token = _PINNED.set(current_registry())
        try:
            return await function(*args, **kwargs)
        finally:
            _PINNED.reset(token)
    return run
