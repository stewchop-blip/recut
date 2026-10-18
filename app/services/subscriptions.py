"""Telegram Stars renewable PRO subscriptions.

The recurring invoice is distinct from legacy one-time credit purchases.
Only Telegram payment receipts grant access. All writes are transactional.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update

from app.database.models import ProSubscription, ProSubscriptionCharge, UserSettings
from app.database.repositories import UserSettingsRepository

PERIOD_SECONDS = 2592000


class SubscriptionRejected(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_active(sub, now=None):
    return bool(sub and sub.first_charge_id and aware(sub.expires_at)
                and aware(sub.expires_at) > (now or utcnow()))


async def get_subscription(session, user_id):
    return await session.get(ProSubscription, user_id)


async def prepare_subscription(session, user_id, settings):
    if not settings.pro_subscriptions_enabled:
        raise SubscriptionRejected("Продажи PRO пока отключены.")
    # Lock the existing user settings row to serialize concurrent checkout clicks.
    row = await UserSettingsRepository(session).get_or_create(user_id)
    await session.execute(update(UserSettings).where(UserSettings.id == row.id).values(
        telegram_user_id=user_id))
    sub = await session.get(ProSubscription, user_id)
    if is_active(sub) or (sub and sub.status == "active" and sub.auto_renew):
        raise SubscriptionRejected("Подписка уже оформлена. Управление: /pro")
    if sub and sub.stars == settings.pro_price_stars and (
        utcnow() - aware(sub.created_at)
    ) < timedelta(minutes=55):
        if sub.status == "pending":
            return sub
        if sub.status == "checkout_pending" and sub.checkout_at and (
            utcnow() - aware(sub.checkout_at)) > timedelta(minutes=2):
            sub.status = "pending"
            sub.checkout_at = None
            return sub
        if sub.status == "checkout_pending":
            raise SubscriptionRejected("Платёж обрабатывается. Проверь статус через /pro.")
    if sub is None:
        sub = ProSubscription(telegram_user_id=user_id)
        session.add(sub)
    sub.invoice_payload = "pro:" + uuid4().hex
    sub.stars = settings.pro_price_stars
    sub.first_charge_id = None
    sub.expires_at = None
    sub.period_start = None
    sub.auto_renew = True
    sub.status = "pending"
    sub.checkout_at = None
    sub.created_at = utcnow()
    await session.flush()
    return sub


async def validate_subscription_checkout(session, payload, user_id, currency, amount, settings):
    if not payload.startswith("pro:") or not settings.pro_subscriptions_enabled:
        raise SubscriptionRejected("Подписка недоступна.")
    sub = await session.get(ProSubscription, user_id)
    if not sub or sub.invoice_payload != payload or sub.status != "pending":
        raise SubscriptionRejected("Создай новый счёт через /pro.")
    if aware(sub.created_at) < utcnow() - timedelta(hours=1):
        raise SubscriptionRejected("Срок действия счёта истёк.")
    if currency != "XTR" or amount != sub.stars:
        raise SubscriptionRejected("Неверная сумма платежа.")
    # Commit before Telegram receives pre-checkout approval. It is not a charge.
    updated = await session.execute(update(ProSubscription).where(
        ProSubscription.telegram_user_id == user_id, ProSubscription.status == "pending"
    ).values(status="checkout_pending", checkout_at=utcnow()))
    if updated.rowcount != 1:
        raise SubscriptionRejected("Подписка уже оформляется.")
    return sub


async def record_subscription_payment(session, user_id, payment):
    if payment.currency != "XTR" or not payment.is_recurring:
        raise SubscriptionRejected("Invalid recurring receipt")
    exp = payment.subscription_expiration_date
    if not exp:
        raise SubscriptionRejected("Missing subscription expiry")
    expiry = datetime.fromtimestamp(exp, timezone.utc)
    if expiry < utcnow() - timedelta(days=1) or expiry > utcnow() + timedelta(days=32):
        raise SubscriptionRejected("Unexpected subscription expiry")
    charge = payment.telegram_payment_charge_id
    if not charge:
        raise SubscriptionRejected("Missing charge ID")
    existing = await session.get(ProSubscriptionCharge, charge)
    if existing:
        if existing.telegram_user_id != user_id:
            raise SubscriptionRejected("Charge identity conflict")
        return False
    # Lock the row so that two receipts cannot overwrite a newer expiry.
    sub = await session.scalar(select(ProSubscription).where(
        ProSubscription.telegram_user_id == user_id).with_for_update())
    if not sub or sub.invoice_payload != payment.invoice_payload or sub.stars != payment.total_amount:
        raise SubscriptionRejected("Unknown recurring invoice")
    if sub.first_charge_id is None:
        if sub.status != "checkout_pending" or not payment.is_first_recurring:
            raise SubscriptionRejected("Unexpected first recurring payment")
        sub.first_charge_id = charge
    elif payment.is_first_recurring:
        raise SubscriptionRejected("Unexpected second first payment")
    session.add(ProSubscriptionCharge(
        charge_id=charge, telegram_user_id=user_id, invoice_payload=sub.invoice_payload,
        stars=sub.stars, expires_at=expiry, refunded=False,
    ))
    if not sub.expires_at or expiry > aware(sub.expires_at):
        sub.expires_at = expiry
        sub.period_start = expiry - timedelta(seconds=PERIOD_SECONDS)
    sub.status = "active"
    sub.checkout_at = None
    user = await UserSettingsRepository(session).get_or_create(user_id)
    if not user.premium_until or aware(user.premium_until) < expiry:
        user.premium_until = expiry
    await session.flush()
    return True


async def refund_subscription_payment(session, user_id, payment):
    if payment.currency != "XTR":
        raise SubscriptionRejected("Invalid refund currency")
    charge = await session.get(ProSubscriptionCharge, payment.telegram_payment_charge_id)
    if not charge or charge.telegram_user_id != user_id or (
        charge.invoice_payload != payment.invoice_payload or charge.stars != payment.total_amount
    ):
        raise SubscriptionRejected("Unknown refunded charge")
    if charge.refunded:
        return False
    sub = await session.scalar(select(ProSubscription).where(
        ProSubscription.telegram_user_id == user_id).with_for_update())
    if not sub:
        raise SubscriptionRejected("Unknown subscription")
    previous = aware(sub.expires_at)
    charge.refunded = True
    await session.flush()
    latest = await session.scalar(select(func.max(ProSubscriptionCharge.expires_at)).where(
        ProSubscriptionCharge.telegram_user_id == user_id,
        ProSubscriptionCharge.invoice_payload == charge.invoice_payload,
        ProSubscriptionCharge.refunded.is_(False),
    ))
    if sub.invoice_payload == charge.invoice_payload:
        sub.expires_at = aware(latest)
        sub.period_start = (aware(latest) - timedelta(seconds=PERIOD_SECONDS)) if latest else None
        if not latest:
            sub.status = "refunded"
            sub.auto_renew = False
        user = await UserSettingsRepository(session).get(user_id)
        if user and aware(user.premium_until) == previous:
            user.premium_until = aware(latest)
    return True


async def update_renewal_status(session, user_id, canceled):
    sub = await session.get(ProSubscription, user_id)
    if not is_active(sub):
        raise SubscriptionRejected("Нет активной подписки.")
    sub.auto_renew = not canceled
    return sub
