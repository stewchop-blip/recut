"""Unified appearance settings; extra controls never reach the media action menu."""
import asyncio
import tempfile
from pathlib import Path

from aiogram import F, Router, types

from app.database.repositories import UserSettingsRepository
from app.database.session import db_manager
from app.services.appearance import download_asset, effective_branding, is_premium, validate_image, MAX_ASSET_BYTES
from app.core.logging import get_logger

router = Router()
logger = get_logger(__name__)
_awaiting_logo: set[int] = set()


def keyboard(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows])


def appearance_view(s):
    on = lambda value: 'ВКЛ' if value else 'ВЫКЛ'
    labels = {'clean': 'Чистый', 'meme': 'Мем', 'brand': 'Бренд', 'custom': 'Свой'}
    return ('⚙️ <b>Оформление</b>\n\nНастройки сохраняются и применяются к следующим роликам.', keyboard([
        [(f"Стиль: {labels.get(s.style_id, 'Свой')}", 'style:pick')],
        [(f'💬 Субтитры: {on(s.subtitles_enabled)}', 'settings:toggle_subs')],
        [('Стиль и язык субтитров', 'appearance:subtitles')],
        [('🖼 Баннер', 'banner:menu'), ('🏷 Логотип', 'logo:menu')],
        [('📐 Формат', 'appearance:format'), ('Качество', 'appearance:quality')],
        [(f'ReCut branding: {on(effective_branding(s))}', 'style:brand')],
        [('Обработка и звук', 'appearance:processing')],
        [('Другие настройки', 'fine:menu')],
        [('⬅️ К видео', 'settings:back'), ('🏠 Главное меню', 'home:open')],
    ]))


async def load(user_id):
    async with db_manager.session() as session:
        return await UserSettingsRepository(session).get_or_create(user_id)


@router.callback_query(F.data == 'appearance:menu')
async def show_appearance(call):
    s = await load(call.from_user.id)
    text, markup = appearance_view(s)
    await call.answer()
    await call.message.edit_text(text, parse_mode='HTML', reply_markup=markup)


CHOICES = {
    'format': ('Формат ролика', 'output_mode', [('universal_9_16', '9:16'), ('portrait_4_5', '4:5'), ('square', '1:1')]),
    'quality': ('Качество ролика', 'output_quality', [('standard', 'Высокое'), ('compact', 'Компактный файл')]),
    'processing': ('Обработка', 'processing_style', [('standard', 'Аккуратная'), ('maximum', 'Выразительная')]),
    'subtitle_style': ('Стиль субтитров', 'subtitle_style', [('standard', 'Белые с обводкой'), ('large', 'Крупные')]),
    'language': ('Язык речи', 'subtitle_language', [('', 'Определять автоматически'), ('ru', 'Русский'), ('en', 'Английский')]),
}


@router.callback_query(F.data.in_({f'appearance:{key}' for key in CHOICES}))
async def show_choices(call):
    key = call.data.split(':')[1]
    title, field, choices = CHOICES[key]
    s = await load(call.from_user.id)
    rows = [[(('✅ ' if getattr(s, field) == value else '') + label, f'appearance_set:{key}:{value}')] for value, label in choices]
    if key == 'processing':
        rows += [[('🔊 Звук', 'audio:menu')], [('✂️ Нарезать длинное видео', 'mode:moments')], [('Сделать 3 варианта', 'action:versions')]]
    rows += [[('⬅️ Назад', 'appearance:menu')]]
    await call.answer()
    await call.message.edit_text(title, reply_markup=keyboard(rows))


@router.callback_query(F.data == 'appearance:subtitles')
async def subtitle_options(call):
    await call.answer()
    await call.message.edit_text('Настройки субтитров', reply_markup=keyboard([
        [('Стиль', 'appearance:subtitle_style'), ('Язык речи', 'appearance:language')],
        [('⬅️ Назад', 'appearance:menu')]]))


@router.callback_query(F.data.startswith('appearance_set:'))
async def save_choice(call):
    _, key, value = call.data.split(':', 2)
    if key not in CHOICES or value not in {v for v, _ in CHOICES[key][2]}:
        await call.answer('Неизвестная настройка', show_alert=True)
        return
    async with db_manager.session() as session:
        await UserSettingsRepository(session).update_fields(call.from_user.id, **{CHOICES[key][1]: value})
    await show_appearance(call)


@router.callback_query(F.data == 'settings:toggle_subs')
async def toggle_subtitles(call):
    async with db_manager.session() as session:
        repo = UserSettingsRepository(session)
        s = await repo.get_or_create(call.from_user.id)
        s.subtitles_enabled = not s.subtitles_enabled
    await show_appearance(call)


@router.callback_query(F.data == 'style:brand')
async def toggle_branding(call):
    async with db_manager.session() as session:
        repo = UserSettingsRepository(session)
        s = await repo.get_or_create(call.from_user.id)
        if not is_premium(s):
            await call.answer()
            await call.message.edit_text('Убрать ReCut с готовых видео можно с Premium.', reply_markup=keyboard([
                [('Получить Premium', 'premium:info')], [('⬅️ Назад', 'appearance:menu')]]))
            return
        s.recut_branding = not s.recut_branding
    await show_appearance(call)


@router.callback_query(F.data == 'premium:info')
async def premium_info(call):
    await call.answer()
    await call.message.edit_text('Premium пока не доступен для покупки. По вопросам доступа: @stewchop.\n'
        'Пакеты обработок не отключают ReCut branding.', reply_markup=keyboard([[('⬅️ Назад', 'appearance:menu')]]))


@router.callback_query(F.data.startswith('logo:'))
async def logo_controls(call):
    user_id = call.from_user.id
    action = call.data.split(':')[1]
    s = await load(user_id)
    if action == 'upload':
        _awaiting_logo.add(user_id)
        from app.bot.handlers.video import _awaiting_banner
        _awaiting_banner.discard(user_id)
        await call.answer()
        await call.message.edit_text('Отправь логотип: PNG, JPEG или WebP, до 10 МБ.\n'
            'Для прозрачного фона отправь PNG как файл.', reply_markup=keyboard([[('Отмена', 'logo:cancel')]]))
        return
    if action == 'preview' and s.logo_telegram_file_id:
        await call.answer()
        try:
            with tempfile.TemporaryDirectory(prefix='recut_logo_preview_') as directory:
                path = await download_asset(call.bot, s.logo_telegram_file_id, Path(directory)/'logo.png')
                await asyncio.to_thread(validate_image, path)
                await call.message.answer_document(types.FSInputFile(path), caption='Твой логотип')
        except Exception:
            await call.message.answer('Не удалось показать логотип. Попробуй ещё раз.')
        return
    if action in {'delete', 'toggle'}:
        async with db_manager.session() as session:
            repo = UserSettingsRepository(session)
            s = await repo.get_or_create(user_id)
            if action == 'delete':
                s.logo_telegram_file_id = None
                s.logo_enabled = False
            elif s.logo_telegram_file_id:
                s.logo_enabled = not s.logo_enabled
    _awaiting_logo.discard(user_id)
    rows = [[('Заменить логотип' if s.logo_telegram_file_id else 'Загрузить логотип', 'logo:upload')]]
    if s.logo_telegram_file_id:
        rows += [[('Посмотреть', 'logo:preview')], [(f"Использовать: {'ВКЛ' if s.logo_enabled else 'ВЫКЛ'}", 'logo:toggle')], [('Удалить', 'logo:delete')]]
    rows += [[('⬅️ Назад', 'appearance:menu')]]
    await call.answer()
    await call.message.edit_text('🏷 Логотип\nСохраняется и добавляется к следующим роликам.', reply_markup=keyboard(rows))


@router.message(lambda message: message.from_user and message.from_user.id in _awaiting_logo)
async def upload_logo(message, bot):
    file = message.document or (message.photo[-1] if message.photo else None)
    if not file or (file.file_size or 0) > MAX_ASSET_BYTES:
        await message.answer('Отправь PNG, JPEG или WebP до 10 МБ.')
        return
    try:
        with tempfile.TemporaryDirectory(prefix='recut_logo_') as directory:
            path = Path(directory) / 'asset'
            await download_asset(bot, file.file_id, path)
            await asyncio.to_thread(validate_image, path)
        async with db_manager.session() as session:
            await UserSettingsRepository(session).update_fields(message.from_user.id,
                logo_telegram_file_id=file.file_id, logo_enabled=True)
    except Exception as exc:
        logger.warning('logo_upload_failed', user_id=message.from_user.id, error_type=type(exc).__name__)
        await message.answer('Не удалось сохранить логотип. Проверь формат и размер изображения.')
        return
    _awaiting_logo.discard(message.from_user.id)
    await message.answer('✅ Логотип сохранён. Он применяется к следующим роликам.',
        reply_markup=keyboard([[('⚙️ Оформление', 'appearance:menu')]]))
