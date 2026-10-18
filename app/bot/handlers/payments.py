"""Dormant Stars UI. Receipts remain active when sales are switched off."""
import asyncio

from aiogram import F, Router, types
from aiogram.filters import Command
from datetime import timezone
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
                if query.invoice_payload.startswith("pro:"):
                    from app.services.subscriptions import validate_subscription_checkout
                    await validate_subscription_checkout(session, query.invoice_payload,
                        query.from_user.id, query.currency, query.total_amount, get_settings())
                else:
                    await check_checkout(session, query.invoice_payload, query.from_user.id,
                                         query.currency, query.total_amount, get_settings())
    except Exception:
        await query.answer(ok=False, error_message="Оплата недоступна. Попробуй позже.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful(message: types.Message):
    payment = message.successful_payment
    if payment.invoice_payload.startswith("pro:"):
        from app.services.subscriptions import record_subscription_payment
        try:
            async with db_manager.session() as session:
                fresh = await record_subscription_payment(session, message.from_user.id, payment)
            if fresh:
                await message.answer("✅ ReCut PRO активен! Подписка: /pro")
        except Exception as exc:
            logger.error("pro_payment_reconciliation_required", error_type=type(exc).__name__)
            await message.answer("Платёж требует проверки. Обратись в /paysupport с чеком.")
            raise
        return
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
    if payment.invoice_payload.startswith("pro:"):
        from app.services.subscriptions import refund_subscription_payment
        async with db_manager.session() as session:
            await refund_subscription_payment(session, message.from_user.id, payment)
        return
    async with db_manager.session() as session:
        await record_refund(session, payment.invoice_payload, payment.currency,
                            payment.total_amount, payment.telegram_payment_charge_id)


@router.message(Command("balance"))
async def show_balance(message: types.Message):
    if not message.from_user or message.chat.type != "private":
        return
    settings = get_settings()
    from app.services.generations import free_remaining
    from app.services.subscriptions import get_subscription, is_active
    async with db_manager.session() as session:
        credits = await balance(session, message.from_user.id)
        remaining = await free_remaining(session, message.from_user.id, settings)
        paid_pro = is_active(await get_subscription(session, message.from_user.id))
    day_limit = settings.pro_daily_generations if paid_pro else settings.daily_free_generations
    details = (f"Доступно обычных обработок сегодня: {remaining} из {day_limit}. "
               "Лимит обновляется в 03:00 по Минску. Затем используется бонусный баланс. "
               "Один запуск, включая три варианта, — одна обработка. Приглашения: /referral"
               if not settings.has_unlimited_generations(message.from_user.id) else
               "Для твоего аккаунта обработка безлимитная. Бонусы сохраняются на балансе.")
    await message.answer(f"Баланс дополнительных обработок: {credits}\n\n{details}")


@router.message(Command("terms"))
async def terms(message: types.Message):
    settings = get_settings()
    pro_terms = (
        f"ReCut PRO: {settings.pro_price_stars} Telegram Stars каждые 30 дней. "
        "Автоматическое списание повторяется, пока продление не отменено. "
        f"В течение каждого оплаченного периода доступно до {settings.pro_ai_videos_per_period} "
        f"AI-роликов и {settings.pro_daily_generations} обычных обработок в день. "
        "Доступ привязан к Telegram аккаунту покупателя. "
        "Отменить новые списания можно командой /pro с сохранением доступа "
        "до конца оплаченного периода. Изменения тарифа действуют для новых покупок. "
        f"Поддержка платежей и вопросы возврата: {settings.payment_support}. "
        "Возвраты обрабатываются поддержкой с учётом правил Telegram."
    )
    body = pro_terms + ("\n\n" + settings.payment_terms if settings.payment_terms else "")
    await message.answer(body, parse_mode=None)


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


# === Renewable PRO subscriptions (kept separate from legacy /buy credits) ===

def _pro_keyboard(sub, enabled, active):
    rows = []
    if active:
        if sub.auto_renew:
            rows.append([types.InlineKeyboardButton(text="❌ Отменить автопродление",
                                                     callback_data="pro:cancel")])
        else:
            rows.append([types.InlineKeyboardButton(text="🔄 Возобновить автопродление",
                                                     callback_data="pro:resume")])
    elif enabled:
        rows.append([types.InlineKeyboardButton(text="⭐ Оформить PRO", callback_data="pro:buy")])
    rows.append([types.InlineKeyboardButton(text="⬅️ Оформление", callback_data="appearance:menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_pro(bot, user_id):
    from app.services.subscriptions import get_subscription, is_active, aware
    settings = get_settings()
    async with db_manager.session() as session:
        sub = await get_subscription(session, user_id)
        active = is_active(sub)
    if active:
        date = aware(sub.expires_at).astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        renewal = ("Автопродление включено" if sub.auto_renew else
                   "Автопродление отключено, повторных списаний не будет")
        text = (f"👑 <b>ReCut PRO активен</b>\n\n"
                f"Текущий период до: {date}\n{renewal}\n"
                f"AI-роликов: {settings.pro_ai_videos_per_period} за 30 дней.\n"
                f"Обычных обработок: до {settings.pro_daily_generations} в день.")
    else:
        text = (f"⭐ <b>ReCut PRO</b>\n\n"
                f"{settings.pro_price_stars} Stars каждые 30 дней с автопродлением.\n"
                f"До {settings.pro_ai_videos_per_period} AI-роликов за период, "
                f"до {settings.pro_daily_generations} обычных обработок в день, "
                "без обязательного брендинга ReCut.\n\n"
                "Отменить автоматическое продление можно в самом боте; "
                "PRO останется до конца оплаченного периода.\n"
                f"Поддержка: {settings.payment_support}\n"
                + ("Продажи пока не открыты." if not settings.pro_subscriptions_enabled else
                   "Нажимая «Оформить», ты соглашаешься с условиями /terms."))
    return text, _pro_keyboard(sub, settings.pro_subscriptions_enabled, active)


@router.message(Command("pro"))
async def pro_info(message: types.Message):
    if not message.from_user or message.chat.type != "private":
        return
    text, keyboard = await _show_pro(message.bot, message.from_user.id)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "pro:show")
async def pro_show(callback: types.CallbackQuery):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    text, keyboard = await _show_pro(callback.bot, callback.from_user.id)
    await callback.answer()
    await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "pro:buy")
async def pro_buy(callback: types.CallbackQuery):
    from app.services.subscriptions import prepare_subscription, SubscriptionRejected, PERIOD_SECONDS
    settings = get_settings()
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Покупка доступна только в личном чате.", show_alert=True)
        return
    try:
        async with db_manager.session() as session:
            sub = await prepare_subscription(session, callback.from_user.id, settings)
        url = await callback.bot.create_invoice_link(
            title="ReCut PRO · 30 дней",
            description=f"Подписка, до {settings.pro_ai_videos_per_period} AI-роликов за 30 дней. Автопродление, отмена: /pro",
            payload=sub.invoice_payload, provider_token="", currency="XTR",
            prices=[types.LabeledPrice(label="ReCut PRO", amount=sub.stars)],
            subscription_period=PERIOD_SECONDS,
        )
    except SubscriptionRejected as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("pro_invoice_creation_failed")
        await callback.answer("Не удалось подготовить оплату. Попробуй позже.", show_alert=True)
        return
    await callback.answer()
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"Оплатить {sub.stars} ⭐ / 30 дней", url=url)],
        [types.InlineKeyboardButton(text="⬅️ Мой PRO", callback_data="pro:show")],
    ])
    await callback.message.answer(
        f"👑 <b>Подписка ReCut PRO</b>\n\n"
        f"{sub.stars} ⭐ каждые 30 дней, если не отменить продление.\n"
        "Отмена: /pro, доступ сохраняется до конца оплаченного периода.\n"
        f"Поддержка: {settings.payment_support}\n"
        "Условия: /terms", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "pro:cancel")
async def pro_cancel(callback: types.CallbackQuery):
    if not callback.message:
        await callback.answer()
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="Да, отключить продление", callback_data="pro:confirm_cancel")],
        [types.InlineKeyboardButton(text="Нет, оставить", callback_data="pro:show")],
    ])
    await callback.answer()
    await callback.message.answer("Отключить следующие списания? Текущий оплаченный срок PRO сохранится.", reply_markup=keyboard)


async def _switch_renewal(callback: types.CallbackQuery, canceled: bool):
    from app.services.subscriptions import get_subscription, is_active, update_renewal_status
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Только в личном чате.", show_alert=True)
        return
    async with db_manager.session() as session:
        sub = await get_subscription(session, callback.from_user.id)
        if not is_active(sub) or not sub.first_charge_id:
            await callback.answer("Активная подписка не найдена.", show_alert=True)
            return
        charge_id = sub.first_charge_id
    try:
        await callback.bot.edit_user_star_subscription(
            user_id=callback.from_user.id, telegram_payment_charge_id=charge_id,
            is_canceled=canceled)
        async with db_manager.session() as session:
            await update_renewal_status(session, callback.from_user.id, canceled)
    except Exception:
        logger.exception("pro_renewal_update_failed")
        await callback.answer("Не удалось изменить подписку. Попробуй позже или напиши /paysupport.", show_alert=True)
        return
    await callback.answer("Автопродление отключено." if canceled else "Автопродление включено.")
    text, keyboard = await _show_pro(callback.bot, callback.from_user.id)
    await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "pro:confirm_cancel")
async def pro_confirm_cancel(callback: types.CallbackQuery):
    await _switch_renewal(callback, True)


@router.callback_query(F.data == "pro:resume")
async def pro_resume(callback: types.CallbackQuery):
    await _switch_renewal(callback, False)


@router.message(Command("refund_pro"))
async def refund_pro(message: types.Message):
    """Admin-only refund of the latest non-refunded PRO renewal charge."""
    from types import SimpleNamespace
    from app.database.models import ProSubscriptionCharge
    from app.services.subscriptions import refund_subscription_payment, get_subscription, is_active

    settings = get_settings()
    admins = {int(x.strip()) for x in settings.payment_admin_ids.split(",") if x.strip().isdigit()}
    if not message.from_user or message.from_user.id not in admins or message.chat.type != "private":
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /refund_pro TELEGRAM_ID")
        return
    user_id = int(parts[1])
    async with db_manager.session() as session:
        charge = await session.scalar(select(ProSubscriptionCharge).where(
            ProSubscriptionCharge.telegram_user_id == user_id,
            ProSubscriptionCharge.refunded.is_(False),
        ).order_by(ProSubscriptionCharge.expires_at.desc()).limit(1))
        sub = await get_subscription(session, user_id)
        first_charge_id = sub.first_charge_id if sub and is_active(sub) and sub.auto_renew else None
    if not charge:
        await message.answer("Нет оплаченного PRO-периода для возврата.")
        return
    if first_charge_id:
        try:
            await message.bot.edit_user_star_subscription(
                user_id=user_id, telegram_payment_charge_id=first_charge_id,
                is_canceled=True)
        except Exception:
            logger.warning("pro_refund_cancellation_requires_check", user_id=user_id)
    try:
        await message.bot.refund_star_payment(user_id=user_id,
                                              telegram_payment_charge_id=charge.charge_id)
    except Exception:
        logger.exception("pro_refund_telegram_state_uncertain", user_id=user_id)
        await message.answer("Не удалось подтвердить возврат. Проверь историю Stars и /paysupport.")
        return
    receipt = SimpleNamespace(invoice_payload=charge.invoice_payload,
                              total_amount=charge.stars, currency="XTR",
                              telegram_payment_charge_id=charge.charge_id)
    async with db_manager.session() as session:
        await refund_subscription_payment(session, user_id, receipt)
        if first_charge_id:
            sub = await get_subscription(session, user_id)
            if sub:
                sub.auto_renew = False
    await message.answer("✅ Возврат последнего платежа PRO обработан.")
