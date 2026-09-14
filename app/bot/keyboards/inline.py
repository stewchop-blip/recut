"""Inline keyboards — minimal, no dead buttons.

The previous version exposed four buttons after each audio result
("Сгенерировать снова", "Сменить голос", "Подготовить текст", "Новый
текст") but only two of them actually worked. Users clicked the dead
ones and got confusing errors. Now we only show buttons that do
something useful.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Voice selection — shown right after the user sends text
VOICE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🎙 Мужской", callback_data="voice:male"),
        InlineKeyboardButton(text="🎙 Женский", callback_data="voice:female"),
        InlineKeyboardButton(text="⚡ Энергичный", callback_data="voice:energetic"),
    ]
])

# After audio is sent: just let the user start over with a new text
RESULT_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(
            text="🎙 Озвучить другим голосом",
            callback_data="action:change_voice",
        )
    ]
])

# Start menu — single button to remind the user what to do
START_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Что умеет бот", callback_data="start:help")]
])


def get_voice_keyboard() -> InlineKeyboardMarkup:
    return VOICE_KEYBOARD


def get_result_keyboard() -> InlineKeyboardMarkup:
    return RESULT_KEYBOARD


def get_rewrite_keyboard() -> InlineKeyboardMarkup:
    # Rewrite flow is not used in MVP — keep a placeholder so imports
    # in rewrite_flow.py don't break.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Закрыть", callback_data="rewrite:close")]
    ])