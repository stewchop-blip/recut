"""Stars invoice persistence. Every mutation runs in the caller's DB transaction."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CreditEntry, PaymentOrder


class PaymentRejected(ValueError):
    pass


def sales_available(settings) -> bool:
    return bool(settings.payments_enabled and settings.payment_price_stars > 0
                and settings.payment_credits > 0 and settings.payment_terms.strip()
                and len(settings.payment_terms) <= 3000 and settings.payment_support.strip())


async def create_order(session: AsyncSession, user_id: int, settings) -> PaymentOrder:
    if not sales_available(settings):
        raise PaymentRejected("Payments are disabled")
    order = PaymentOrder(id=uuid4().hex, telegram_user_id=user_id,
                         stars=settings.payment_price_stars, credits=settings.payment_credits,
                         terms=settings.payment_terms, status="pending")
    session.add(order)
    await session.flush()
    return order


async def owned_order(session, order_id, user_id):
    order = await session.get(PaymentOrder, order_id)
    if order is None or order.telegram_user_id != user_id:
        raise PaymentRejected("Unknown order")
    return order


def check_receipt(order, currency, amount):
    if currency != "XTR" or amount != order.stars:
        raise PaymentRejected("Receipt mismatch")


def check_fresh(order):
    created = order.created_at.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - created).total_seconds() > 3600:
        raise PaymentRejected("Invoice expired")


async def accept_terms(session, order_id, user_id, settings):
    if not sales_available(settings):
        raise PaymentRejected("Payments are disabled")
    order = await owned_order(session, order_id, user_id)
    check_fresh(order)
    if order.terms != settings.payment_terms or order.status != "pending":
        raise PaymentRejected("Create a new order")
    result = await session.execute(update(PaymentOrder).where(
        PaymentOrder.id == order.id, PaymentOrder.status == "pending"
    ).values(status="invoiced"))
    if result.rowcount != 1:
        raise PaymentRejected("Invoice already issued")
    return order


async def check_checkout(session, order_id, user_id, currency, amount, settings):
    if not sales_available(settings) or user_id not in settings.allowed_user_id_set:
        raise PaymentRejected("Payments are unavailable")
    order = await owned_order(session, order_id, user_id)
    check_receipt(order, currency, amount)
    check_fresh(order)
    if order.status != "invoiced":
        raise PaymentRejected("Order is not payable")


async def record_payment(session, order_id, user_id, currency, amount, charge_id):
    # Never gate an already completed payment on current flags, access or expiry.
    order = await owned_order(session, order_id, user_id)
    check_receipt(order, currency, amount)
    if not charge_id or len(charge_id) > 255:
        raise PaymentRejected("Invalid charge")
    if order.charge_id == charge_id and order.status in {"paid", "refund_pending", "refunded"}:
        return False
    if order.status != "invoiced":
        raise PaymentRejected("Unexpected payment; reconcile with Telegram")
    result = await session.execute(update(PaymentOrder).where(
        PaymentOrder.id == order_id, PaymentOrder.status == "invoiced"
    ).values(status="paid", charge_id=charge_id))
    if result.rowcount != 1:
        # A concurrent transaction won. Verify identity instead of crediting again.
        await session.refresh(order)
        if order.charge_id != charge_id:
            raise PaymentRejected("Conflicting payment")
        return False
    session.add(CreditEntry(key=f"payment:{order_id}", telegram_user_id=user_id,
                           amount=order.credits))
    await session.flush()
    return True


async def record_refund(session, order_id, currency, amount, charge_id):
    # Refund messages may be sent by Telegram itself; charge/order identify owner.
    order = await session.get(PaymentOrder, order_id)
    if order is None:
        raise PaymentRejected("Unknown refund")
    check_receipt(order, currency, amount)
    if not charge_id or (order.charge_id and order.charge_id != charge_id):
        raise PaymentRejected("Refund mismatch")
    if order.status == "refunded":
        return False
    if order.status not in {"invoiced", "paid", "refund_pending"}:
        raise PaymentRejected("Unexpected refund")
    previous = order.status
    result = await session.execute(update(PaymentOrder).where(
        PaymentOrder.id == order_id, PaymentOrder.status == previous
    ).values(status="refunded", charge_id=charge_id))
    if result.rowcount != 1:
        # Retry after rollback rather than silently losing a racing refund.
        raise PaymentRejected("Concurrent refund; retry")
    if previous in {"paid", "refund_pending"}:
        session.add(CreditEntry(key=f"refund:{order_id}", telegram_user_id=order.telegram_user_id,
                               amount=-order.credits))
    await session.flush()
    return True


async def balance(session, user_id):
    return int(await session.scalar(select(func.coalesce(func.sum(CreditEntry.amount), 0)).where(
        CreditEntry.telegram_user_id == user_id)) or 0)
