"""Dormant Stars UI. Receipts remain active when sales are switched off."""
import asyncio

from aiogram import F, Router, types
from aiogram.filters import Command
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import get_logger
from app.database.models import PaymentOrder
from app.database.session import db_manager
from app.services.payments import (
    PaymentRejected, accept_terms, balance, check_checkout, create_order,
    record_payment, record_refund, sales_available,
)

router = Router(name="payments")
logger = get_logger(__name__)
UNAVAILABLE = "Оплата пока недоступна."


@router.message(Command("buy"))
async def buy(message: types.Message):
    settings = get_settings()
    if not sales_available(settings) or message.chat.type != "private" or not message.from_user:
        await message.answer(UNAVAILABLE)
        return
    async with db_manager.session() as session:
        order = await create_order(session, message.from_user.id, settings)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="Принимаю условия, перейти к оплате",
                                   callback_data=f"pay_accept:{order.id}")]])
    await message.answer(
        f"Пакет: {order.credits} обработок за {order.stars} Stars.\n\n{order.terms}",
        parse_mode=None, reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("pay_accept:"))
async def accept(callback: types.CallbackQuery):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer(UNAVAILABLE, show_alert=True)
        return
    try:
        async with db_manager.session() as session:
            order = await accept_terms(session, callback.data.split(":", 1)[1],
                                       callback.from_user.id, get_settings())
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id, title="ReCut: пакет обработок",
            description=f"{order.credits} обработок видео", payload=order.id,
            currency="XTR", provider_token="", start_parameter=f"order_{order.id}",
            prices=[types.LabeledPrice(label="Пакет обработок", amount=order.stars)],
        )
    except PaymentRejected:
        await callback.answer("Счёт недоступен. Создай новый через /buy.", show_alert=True)
        return
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    try:
        async with asyncio.timeout(7):
            async with db_manager.session() as session:
                await check_checkout(session, query.invoice_payload, query.from_user.id,
                                     query.currency, query.total_amount, get_settings())
    except Exception:
        await query.answer(ok=False, error_message="Оплата недоступна. Попробуй позже.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful(message: types.Message):
    payment = message.successful_payment
    try:
        async with db_manager.session() as session:
            fresh = await record_payment(session, payment.invoice_payload, message.from_user.id,
                                         payment.currency, payment.total_amount,
                                         payment.telegram_payment_charge_id)
    except Exception as exc:
        logger.error("payment_reconciliation_required", error_type=type(exc).__name__,
                     order_id=payment.invoice_payload[:32])
        await message.answer("Платёж требует проверки. Напиши /paysupport и приложи чек.")
        raise
    if fresh:
        await message.answer("✅ Оплата получена, пакет зачислен. Баланс: /balance")


@router.message(F.refunded_payment)
async def refunded(message: types.Message):
    payment = message.refunded_payment
    async with db_manager.session() as session:
        await record_refund(session, payment.invoice_payload, payment.currency,
                            payment.total_amount, payment.telegram_payment_charge_id)


@router.message(Command("balance"))
async def show_balance(message: types.Message):
    async with db_manager.session() as session:
        credits = await balance(session, message.from_user.id)
    await message.answer(f"Баланс обработок: {credits}")


@router.message(Command("terms"))
async def terms(message: types.Message):
    await message.answer(get_settings().payment_terms or UNAVAILABLE, parse_mode=None)


@router.message(Command("paysupport"))
async def support(message: types.Message):
    await message.answer(f"По вопросам оплаты: {get_settings().payment_support}\n"
                         "Приложи чек и опиши проблему. Поддержку покупок оказывает ReCut.",
                         parse_mode=None)


@router.message(Command("refund"))
async def refund(message: types.Message):
    settings = get_settings()
    admins = {int(x.strip()) for x in settings.payment_admin_ids.split(",") if x.strip().isdigit()}
    if not message.from_user or message.from_user.id not in admins or message.chat.type != "private":
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /refund ID_заказа")
        return
    async with db_manager.session() as session:
        order = await session.scalar(select(PaymentOrder).where(PaymentOrder.id == parts[1]))
        if order is None or order.status not in {"paid", "refund_pending"}:
            await message.answer("Нет оплаченного заказа для возврата.")
            return
        await session.execute(update(PaymentOrder).where(PaymentOrder.id == order.id,
            PaymentOrder.status == "paid").values(status="refund_pending"))
    # Keep refund_pending on an uncertain network outcome; receipt or retry reconciles it.
    try:
        await message.bot.refund_star_payment(user_id=order.telegram_user_id,
                                             telegram_payment_charge_id=order.charge_id)
    except Exception:
        await message.answer("Возврат требует проверки в Telegram. Статус сохранён, повтор безопасен.")
        return
    async with db_manager.session() as session:
        await record_refund(session, order.id, "XTR", order.stars, order.charge_id)
    await message.answer("Возврат выполнен.")
