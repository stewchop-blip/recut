"""Inline keyboard builders."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Voice selection keyboard (after text input)
VOICE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🎙 Мужской", callback_data="voice:male"),
        InlineKeyboardButton(text="🎙 Женский", callback_data="voice:female"),
        InlineKeyboardButton(text="⚡ Энергичный", callback_data="voice:energetic"),
    ]
])

# Result actions (after audio sent)
RESULT_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🔄 Сгенерировать снова", callback_data="action:regenerate"),
        InlineKeyboardButton(text="🎙 Сменить голос", callback_data="action:change_voice"),
    ],
    [
        InlineKeyboardButton(text="✨ Подготовить текст", callback_data="action:rewrite"),
        InlineKeyboardButton(text="📝 Новый текст", callback_data="action:new_text"),
    ],
])

# Rewrite result actions
REWRITE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🎙 Озвучить", callback_data="rewrite:voice"),
        InlineKeyboardButton(text="✏️ Использовать оригинал", callback_data="rewrite:original"),
    ],
    [
        InlineKeyboardButton(text="🔄 Переписать ещё раз", callback_data="rewrite:again"),
    ],
])

# Start menu
START_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎧 Начать", callback_data="start:begin")],
])


def get_voice_keyboard() -> InlineKeyboardMarkup:
    return VOICE_KEYBOARD


def get_result_keyboard() -> InlineKeyboardMarkup:
    return RESULT_KEYBOARD


def get_rewrite_keyboard() -> InlineKeyboardMarkup:
    return REWRITE_KEYBOARD