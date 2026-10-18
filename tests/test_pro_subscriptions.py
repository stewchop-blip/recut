"""Transactional PRO subscription tests. Uses local SQLite, never Telegram/Stars."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, ProSubscriptionCharge, UserSettings
from app.services.subscriptions import (
    SubscriptionRejected, get_subscription, is_active, prepare_subscription,
    record_subscription_payment, refund_subscription_payment,
    update_renewal_status, validate_subscription_checkout,
)


@pytest.mark.asyncio
async def test_pro_recurring_checkout_renewal_cancel_and_refund(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cfg = SimpleNamespace(pro_subscriptions_enabled=True, pro_price_stars=399)
        user_id = 12345678
        async with sessions() as session:
            sub = await prepare_subscription(session, user_id, cfg)
            payload = sub.invoice_payload
            await session.commit()
        async with sessions() as session:
            await validate_subscription_checkout(session, payload, user_id, "XTR", 399, cfg)
            with pytest.raises(SubscriptionRejected):
                await validate_subscription_checkout(session, payload, user_id, "XTR", 399, cfg)
            await session.commit()

        expiry = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        first = SimpleNamespace(
            currency="XTR", total_amount=399, invoice_payload=payload,
            is_recurring=True, is_first_recurring=True,
            subscription_expiration_date=expiry, telegram_payment_charge_id="first-charge",
        )
        async with sessions() as session:
            assert await record_subscription_payment(session, user_id, first)
            await session.commit()
        async with sessions() as session:
            assert not await record_subscription_payment(session, user_id, first)
            assert is_active(await get_subscription(session, user_id))
            assert (await session.get(UserSettings, 1)).premium_until is not None
            with pytest.raises(SubscriptionRejected):
                await prepare_subscription(session, user_id, cfg)

        # Simulate the next real billing date; Telegram never pays 60 days in advance.
        import app.services.subscriptions as service
        now_at_renewal = datetime.fromtimestamp(expiry, timezone.utc) + timedelta(seconds=2)
        monkeypatch.setattr(service, "utcnow", lambda: now_at_renewal)
        renewal = SimpleNamespace(
            currency="XTR", total_amount=399, invoice_payload=payload,
            is_recurring=True, is_first_recurring=False,
            subscription_expiration_date=expiry + 2592000,
            telegram_payment_charge_id="renewal-charge",
        )
        async with sessions() as session:
            assert await record_subscription_payment(session, user_id, renewal)
            await update_renewal_status(session, user_id, True)
            await session.commit()
        async with sessions() as session:
            sub = await get_subscription(session, user_id)
            assert not sub.auto_renew
            assert int(sub.expires_at.replace(tzinfo=timezone.utc).timestamp()) == expiry + 2592000
            assert (await session.get(ProSubscriptionCharge, "renewal-charge")).refunded is False

        refund = SimpleNamespace(
            currency="XTR", total_amount=399, invoice_payload=payload,
            telegram_payment_charge_id="renewal-charge",
        )
        async with sessions() as session:
            assert await refund_subscription_payment(session, user_id, refund)
            assert not await refund_subscription_payment(session, user_id, refund)
            await session.commit()
        async with sessions() as session:
            sub = await get_subscription(session, user_id)
            assert int(sub.expires_at.replace(tzinfo=timezone.utc).timestamp()) == expiry
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_pro_access_for_unpaid_invoice_and_invalid_checkout():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        cfg = SimpleNamespace(pro_subscriptions_enabled=True, pro_price_stars=399)
        async with sessions() as session:
            sub = await prepare_subscription(session, 99, cfg)
            assert not is_active(sub)
            with pytest.raises(SubscriptionRejected):
                await validate_subscription_checkout(session, sub.invoice_payload, 99, "XTR", 398, cfg)
            with pytest.raises(SubscriptionRejected):
                await validate_subscription_checkout(session, sub.invoice_payload, 98, "XTR", 399, cfg)
    finally:
        await engine.dispose()
