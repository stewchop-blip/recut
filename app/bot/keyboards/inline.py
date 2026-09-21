"""Inline keyboards for the Quick Prep UX.

Two main menus:
- Initial action menu after video receipt (Quick Prep / Settings / Analyze)
- Settings menu (CTA on/off, position, timing, subtitle on/off, change banner)
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Action menu after the user sends a video
ACTION_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(
            text="🚀 Подготовить видео",
            callback_data="action:quick_prep",
        ),
    ],
    [
        InlineKeyboardButton(
            text="✂️ Найти лучшие моменты",
            callback_data="action:analyze_long",
        ),
        InlineKeyboardButton(
            text="⚙️ Настройки",
            callback_data="action:settings",
        ),
    ],
])


# Settings menu
SETTINGS_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🔘 CTA вкл/выкл", callback_data="settings:toggle_cta"),
    ],
    [
        InlineKeyboardButton(text="📍 Позиция", callback_data="settings:position"),
    ],
    [
        InlineKeyboardButton(text="⏱ Когда показывать", callback_data="settings:timing"),
    ],
    [
        InlineKeyboardButton(text="💬 Субтитры вкл/выкл", callback_data="settings:toggle_subs"),
    ],
    [
        InlineKeyboardButton(text="📎 Загрузить баннер", callback_data="settings:upload_cta"),
    ],
    [
        InlineKeyboardButton(text="🔙 Назад", callback_data="settings:back"),
    ],
])


# CTA position picker
POSITION_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="↖️ Сверху слева", callback_data="cta_pos:top_left"),
        InlineKeyboardButton(text="⬆️ Сверху", callback_data="cta_pos:top"),
        InlineKeyboardButton(text="↗️ Сверху справа", callback_data="cta_pos:top_right"),
    ],
    [
        InlineKeyboardButton(text="↙️ Снизу слева", callback_data="cta_pos:bottom_left"),
        InlineKeyboardButton(text="⬇️ Снизу", callback_data="cta_pos:bottom"),
        InlineKeyboardButton(text="↘️ Снизу справа", callback_data="cta_pos:bottom_right"),
    ],
    [
        InlineKeyboardButton(text="🔙 Назад", callback_data="settings:back"),
    ],
])


# CTA timing picker
TIMING_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🎬 Весь ролик", callback_data="cta_time:full"),
    ],
    [
        InlineKeyboardButton(text="▶️ Первые 3 сек", callback_data="cta_time:start_3"),
        InlineKeyboardButton(text="⏸ Последние 3 сек", callback_data="cta_time:end_3"),
    ],
    [
        InlineKeyboardButton(text="⏸ Последние 5 сек", callback_data="cta_time:end_5"),
    ],
    [
        InlineKeyboardButton(text="🔙 Назад", callback_data="settings:back"),
    ],
])


def preview_keyboard() -> InlineKeyboardMarkup:
    """Shown after CTA preview generation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="preview:save"),
            InlineKeyboardButton(text="🔄 Изменить", callback_data="settings:position"),
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="settings:back"),
        ],
    ])


# Menu shown after URL download: original vs recut
URL_ACTION_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📥 Скачать оригинал", callback_data="url:original"),
    ],
    [
        InlineKeyboardButton(text="🎬 Сделать Recut", callback_data="url:recut"),
    ],
])