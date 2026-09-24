"""Inline keyboards — simplified UX (PART 12-22).

Progressive disclosure:
- HOME: only «Сделать ролик» / «Нарезать длинное видео» / «Оформление»
- «Сделать 3 варианта» lives in ••• Ещё (advanced short-video option)
- ALL styling lives in 🎨 Оформление (summary + presets + плашка +
  тонкая настройка). No duplicated CTA controls anywhere.
- Every submenu has ⬅️ Назад; sections have 🏠 Главное меню.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.overlays.templates import BACKGROUNDS, TITLES


# ---------------------------------------------------------------------------
# HOME — maximum simplicity (PART 13)
# ---------------------------------------------------------------------------

HOME_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Сделать ролик", callback_data="mode:prepare")],
    [InlineKeyboardButton(text="✂️ Нарезать длинное видео", callback_data="mode:moments")],
    [InlineKeyboardButton(text="🎨 Оформление", callback_data="appearance:menu")],
])


def mode_input_menu(mode: str) -> InlineKeyboardMarkup:
    """Menu after video input in the chosen mode."""
    if mode == "versions":
        rows = [[InlineKeyboardButton(text="✨ Сделать 3 версии", callback_data="action:versions")]]
    else:  # prepare / moments
        rows = [[InlineKeyboardButton(
            text="✨ Сделать" if mode == "prepare" else "✂️ Найти моменты",
            callback_data="action:quick_prep" if mode == "prepare" else "action:analyze_long",
        )]]
    rows.append([InlineKeyboardButton(text="🎨 Оформление", callback_data="appearance:menu")])
    rows.append([InlineKeyboardButton(text="••• Ещё", callback_data="more:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ••• Ещё — advanced options for a pending short video (PART 12)
MORE_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎞 Сделать 3 варианта", callback_data="action:versions")],
    [InlineKeyboardButton(text="🔊 Звук", callback_data="audio:menu")],
    [InlineKeyboardButton(text="💬 Субтитры", callback_data="settings:toggle_subs")],
    [InlineKeyboardButton(text="⚙️ Дополнительно", callback_data="fine:menu")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="more:back")],
])


# 🔊 Звук — audio presets (PART 21/22)
def audio_menu(current: str) -> InlineKeyboardMarkup:
    from app.services.media.audio import AUDIO_PRESETS
    rows = []
    for pid, info in AUDIO_PRESETS.items():
        rows.append([InlineKeyboardButton(
            text=("✅ " if pid == current else "") + info["label"],
            callback_data=f"audio_set:{pid}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="more:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 🎨 Оформление — one style section with summary (PART 15/16)
# ---------------------------------------------------------------------------

def appearance_menu(style_id: str, banner: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎭 Стиль: {style_id}", callback_data="style:pick")],
        [InlineKeyboardButton(text=f"🖼 Плашка: {'✅' if banner else 'нет'}", callback_data="banner:menu")],
        [InlineKeyboardButton(text="🔧 Настроить вручную", callback_data="fine:menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
    ])


def style_pick_menu(current: str) -> InlineKeyboardMarkup:
    """Style presets (PART 16)."""
    presets = [
        ("clean", "⚪️ Чистый"),
        ("meme", "😎 Мем"),
        ("brand", "🏷 Бренд"),
        ("custom", "🔧 Свой"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ " if pid == current else "") + label,
            callback_data=f"style_set:{pid}",
        )] for pid, label in presets
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="appearance:menu")]])


# ⚙️ Тонкая настройка — advanced screen (PART 17)
def fine_menu(background_id: str, title_id: str, brand: bool,
              cta_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎨 Фон: {background_id}", callback_data="style:bg")],
        [InlineKeyboardButton(text=f"🏷 Заголовок: {title_id}", callback_data="style:title")],
        [InlineKeyboardButton(text=f"🏷 Бренд-уголок: {'ВКЛ' if brand else 'ВЫКЛ'}", callback_data="style:brand")],
        [InlineKeyboardButton(text=f"🔘 Плашка: {'ВКЛ' if cta_enabled else 'ВЫКЛ'}", callback_data="settings:toggle_cta")],
        [InlineKeyboardButton(text="🖼 Плашка (размер/позиция/время)", callback_data="banner:menu")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="appearance:menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
    ])


# ---------------------------------------------------------------------------
# 🖼 Плашка — own section (PART 18: no duplicates elsewhere)
# ---------------------------------------------------------------------------

def banner_menu(exists: bool) -> InlineKeyboardMarkup:
    if exists:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Загрузить новую", callback_data="banner:upload")],
            [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="banner:preview")],
            [InlineKeyboardButton(text="📍 Положение", callback_data="settings:position")],
            [InlineKeyboardButton(text="📏 Размер", callback_data="settings:size")],
            [InlineKeyboardButton(text="⏱ Время показа", callback_data="settings:timing")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="banner:delete")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="appearance:menu")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Загрузить плашку", callback_data="banner:upload")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="appearance:menu")],
    ])


BANNER_CANCEL_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отмена", callback_data="banner:cancel")],
])


# ---------------------------------------------------------------------------
# Result screens
# ---------------------------------------------------------------------------

RESULT_MENU_PREPARE = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Ещё одно видео", callback_data="mode:prepare")],
    [InlineKeyboardButton(text="🎨 Оформление", callback_data="appearance:menu")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])

RESULT_MENU_VERSIONS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎞 Ещё варианты", callback_data="action:versions")],
    [InlineKeyboardButton(text="🎨 Оформление", callback_data="appearance:menu")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])

RESULT_MENU_MOMENTS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✂️ Ещё одно видео", callback_data="mode:moments")],
    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home:open")],
])


# ---------------------------------------------------------------------------
# Fine-setting pickers (back → fine:menu)
# ---------------------------------------------------------------------------

def background_menu(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ " if bg.id == current else "") + bg.label,
            callback_data=f"style_bg:{bg.id}",
        )] for bg in BACKGROUNDS.values()
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="fine:menu")]])


def title_menu(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ " if t.id == current else "") + t.label,
            callback_data=f"style_title:{t.id}",
        )] for t in TITLES.values()
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="fine:menu")]])


SIZE_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🔷 Маленькая (~28%)", callback_data="cta_size:small"),
        InlineKeyboardButton(text="🔶 Средняя (~33%)", callback_data="cta_size:medium"),
    ],
    [
        InlineKeyboardButton(text="🔸 Большая (~38%)", callback_data="cta_size:large"),
    ],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="banner:menu")],
])

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
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="banner:menu")],
])

TIMING_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Весь ролик", callback_data="cta_time:full")],
    [
        InlineKeyboardButton(text="▶️ Первые 3 сек", callback_data="cta_time:start_3"),
        InlineKeyboardButton(text="⏸ Последние 3 сек", callback_data="cta_time:end_3"),
    ],
    [InlineKeyboardButton(text="⏸ Последние 5 сек", callback_data="cta_time:end_5")],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="banner:menu")],
])


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Изменить", callback_data="banner:menu")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="banner:menu")],
    ])


# Legacy aliases (kept until all references migrate)
ACTION_MENU = mode_input_menu("prepare")


# Menu shown after URL download: original vs recut
URL_ACTION_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📥 Скачать оригинал", callback_data="url:original")],
    [InlineKeyboardButton(text="🎬 Сделать Recut", callback_data="url:recut")],
])
