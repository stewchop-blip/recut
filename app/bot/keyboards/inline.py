"""Inline keyboards.

Recut MVP uses minimal keyboards — most flows reply with a single
caption + status message and rely on text replies. This module will
grow as we add more stages (clip preview, format selection, etc.).
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def start_keyboard() -> InlineKeyboardMarkup:
    """Shown on /start. Single button to acknowledge help text."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Что умеет бот", callback_data="start:help")]
    ])
