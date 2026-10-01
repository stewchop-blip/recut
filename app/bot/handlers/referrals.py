"""Personal referral link and persistent bonus balance; private chats only."""
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.utils.deep_linking import create_start_link

from app.core.config import get_settings
from app.database.session import db_manager
from app.services.payments import balance
from app.services.referrals import stats

router = Router()


async def referral_text(user_id, bot):
    settings = get_settings()
    async with db_manager.session() as session:
        profile, invited, earned = await stats(session, user_id)
        credits = await balance(session, user_id)
        token = profile.invite_token
        rewarded = profile.rewarded_invites
    text = (f'🎁 <b>Пригласи друга</b>\n\n'
            f'Друзей по ссылке: {invited}\n'
            f'Приглашений с бонусом: {rewarded}\n'
            f'Начислено бонусных обработок: {earned}\n'
            f'Доступный баланс: {credits}\n\n')
    if not settings.referrals_enabled:
        return text + 'Реферальная программа готовится к запуску. Приглашения пока не начисляют бонусы.'
    link = await create_start_link(bot, f'ref_{token}')
    text += (f'Получай +{settings.referral_reward_credits} обработки за нового друга, '
             'который впервые откроет бота по твоей ссылке и получит первое обработанное видео. '
             'Один друг — один бонус; скачивание оригинала не считается.\n\n'
             f'Бонусы доступны за первые {settings.referral_max_rewards} таких приглашений.\n\n'
             f'Твоя ссылка:\n{link}\n\n')
    if not settings.generation_limits_enabled:
        text += 'Сейчас обработка безлимитная. Бонусы сохраняются на балансе для будущих лимитов.'
    else:
        text += (f'В день бесплатно: {settings.daily_free_generations} обработки. '
                 'Затем используется бонусный баланс. Один запуск, включая три варианта, — одна обработка.')
    return text


@router.message(Command('referral'))
async def show_referral(message: types.Message):
    if message.chat.type != 'private' or not message.from_user:
        return
    await message.answer(await referral_text(message.from_user.id, message.bot), parse_mode='HTML')


@router.callback_query(F.data == 'referral:show')
async def referral_callback(call: types.CallbackQuery):
    if not call.message or call.message.chat.type != 'private':
        await call.answer('Открой личный чат с ботом.', show_alert=True)
        return
    await call.answer()
    await call.message.answer(await referral_text(call.from_user.id, call.bot), parse_mode='HTML')
