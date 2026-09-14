"""Shared FSM states for the bot."""
from aiogram.fsm.state import State, StatesGroup


class RecutStates(StatesGroup):
    """User is in the 'text received, awaiting voice choice' flow."""
    awaiting_voice = State()  # user text is stored in FSM data
