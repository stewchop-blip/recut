"""Public launch admits new users without granting operator privileges."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.middlewares import access
from app.core.config import Settings


@pytest.mark.parametrize('public,allowed,user_id,admitted', [
    (True, {10}, 20, True),
    (True, set(), 20, True),
    (False, {10}, 20, False),
    (False, {10}, 10, True),
    (False, set(), 20, False),
])
async def test_access_modes(monkeypatch, public, allowed, user_id, admitted):
    monkeypatch.setattr(access, 'get_settings', lambda: SimpleNamespace(
        public_access_enabled=public, allowed_user_id_set=allowed))
    event = SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=AsyncMock())
    handler = AsyncMock()
    data = {}
    await access.AccessMiddleware()(handler, event, data)
    assert handler.await_count == int(admitted)
    if admitted:
        assert data['telegram_user_id'] == user_id
    else:
        event.answer.assert_awaited_once()


def test_public_defaults_preserve_limits_and_operator_roles():
    cfg = Settings(_env_file=None, telegram_bot_token='test', openrouter_api_key='test',
                   database_url='postgresql://test:test@localhost/test',
                   allowed_telegram_user_ids='10,11')
    assert cfg.public_access_enabled
    assert cfg.generation_limits_enabled and cfg.referrals_enabled
    assert cfg.daily_free_generations == 10 and cfg.referral_reward_credits == 3
    assert not cfg.payments_enabled
    assert cfg.has_unlimited_generations(10)
    assert not cfg.has_unlimited_generations(20)
    assert 20 not in cfg.analytics_admin_ids
    assert cfg.analytics_admin_ids == {10, 11}
