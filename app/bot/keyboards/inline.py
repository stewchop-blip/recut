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
            text="🚀 Подготовить к публикации",
            callback_data="action:quick_prep",
        ),
    ],
    [
        InlineKeyboardButton(
            text="✂️ Найти лучшие моменты",
            callback_data="action:analyze_long",
        ),
    ],
    [
        InlineKeyboardButton(
            text="⚙️ Настройки",
            callback_data="action:settings",
        ),
    ],
])

SHORT_ACTION_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(
            text="🚀 Подготовить к публикации",
            callback_data="action:quick_prep",
        ),
    ],
    [
        InlineKeyboardButton(
            text="⚙️ Настройки",
            callback_data="action:settings",
        ),
    ],
])

# ---------------------------------------------------------------------------
# HOME screen (audit: result-named modes, banner is its own section)
# ---------------------------------------------------------------------------

HOME_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🚀 Подготовить к публикации", callback_data="mode:prepare")],
    [InlineKeyboardButton(text="✨ Сделать 3 версии", callback_data="mode:versions")],
    [InlineKeyboardButton(text="✂️ Найти лучшие моменты", callback_data="mode:moments")],
    [InlineKeyboardButton(text="🖼 Плашка", callback_data="banner:menu")],
    [InlineKeyboardButton(text="⚙️ Настройки", callback_data="action:settings")],
])

HOME_BACK_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])

def mode_input_menu(mode: str) -> InlineKeyboardMarkup:
    """Menu shown after video input in the chosen mode (audit #3-5)."""
    if mode == "prepare":
        rows = [[InlineKeyboardButton(text="🚀 Подготовить", callback_data="action:quick_prep")]]
    elif mode == "versions":
        rows = [[InlineKeyboardButton(text="✨ Сделать 3 версии", callback_data="action:versions")]]
    else:  # moments
        rows = [[InlineKeyboardButton(text="✂️ Найти лучшие моменты", callback_data="action:analyze_long")]]
    rows.append([InlineKeyboardButton(text="🖼 Плашка", callback_data="banner:menu")])
    rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="action:settings")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def banner_menu(exists: bool) -> InlineKeyboardMarkup:
    """Banner section: status screen when a banner exists, else empty state."""
    if exists:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Загрузить новую", callback_data="banner:upload")],
            [InlineKeyboardButton(text="📍 Положение", callback_data="settings:position")],
            [InlineKeyboardButton(text="⏱ Время показа", callback_data="settings:timing")],
            [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="banner:preview")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="banner:delete")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Загрузить PNG", callback_data="banner:upload")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
    ])

BANNER_CANCEL_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отмена", callback_data="banner:cancel")],
])

RESULT_MENU_PREPARE = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🚀 Ещё одно видео", callback_data="mode:prepare")],
    [InlineKeyboardButton(text="🖼 Изменить плашку", callback_data="banner:menu")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])

RESULT_MENU_VERSIONS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✨ Сделать ещё варианты", callback_data="mode:versions")],
    [InlineKeyboardButton(text="🖼 Изменить плашку", callback_data="banner:menu")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])

RESULT_MENU_MOMENTS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✂️ Ещё одно видео", callback_data="mode:moments")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
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
        InlineKeyboardButton(text="🖼 Плашка", callback_data="banner:menu"),
    ],
    [
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open"),
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