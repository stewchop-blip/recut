"""Persistent first-touch referrals; all changes belong to caller's transaction."""
import re
import secrets

from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.database.models import BotProfile, CreditEntry, Job, User, UserSettings

TOKEN = re.compile(r"ref_([A-Za-z0-9_-]{24})\Z")


async def ensure_profile(session, user_id: int, payload: str = ""):
    profile = await session.get(BotProfile, user_id)
    if profile is not None:
        return profile, False
    # A pre-existing user cannot become a referral just by opening a new link.
    existed = any([await session.scalar(select(model.telegram_user_id).where(
        model.telegram_user_id == user_id).limit(1)) is not None
        for model in (User, UserSettings, Job)])
    settings = get_settings()
    inviter = None
    match = TOKEN.fullmatch(payload)
    if not existed and settings.referrals_enabled and match:
        inviter = await session.scalar(select(BotProfile).where(
            BotProfile.invite_token == match[1], BotProfile.telegram_user_id != user_id))
    profile = BotProfile(telegram_user_id=user_id, invite_token=secrets.token_urlsafe(18),
        inviter_id=inviter.telegram_user_id if inviter else None,
        reward_credits=settings.referral_reward_credits if inviter else 0)
    try:
        async with session.begin_nested():
            session.add(profile)
            await session.flush()
    except IntegrityError:
        profile = await session.get(BotProfile, user_id)
        if profile is None:
            raise
        return profile, False
    return profile, True


async def qualify_referral(session, user_id: int):
    # Called only after a processed result has been delivered. Honour stored promises
    # even if new referrals were subsequently disabled. No rewards for downloads.
    profile = await session.get(BotProfile, user_id)
    if profile is None or profile.inviter_id is None or profile.referral_qualified:
        return False
    claimed = await session.execute(update(BotProfile).where(
        BotProfile.telegram_user_id == user_id, BotProfile.referral_qualified.is_(False)
    ).values(referral_qualified=True))
    if claimed.rowcount != 1:
        return False
    cap = get_settings().referral_max_rewards
    granted = await session.execute(update(BotProfile).where(
        BotProfile.telegram_user_id == profile.inviter_id,
        BotProfile.rewarded_invites < cap,
    ).values(rewarded_invites=BotProfile.rewarded_invites + 1))
    if granted.rowcount != 1:
        return False
    session.add(CreditEntry(key=f"referral:{user_id}",
        telegram_user_id=profile.inviter_id, amount=profile.reward_credits))
    await session.flush()
    return True


async def stats(session, user_id):
    profile, _ = await ensure_profile(session, user_id)
    invited = await session.scalar(select(func.count()).select_from(BotProfile).where(
        BotProfile.inviter_id == user_id))
    earned = await session.scalar(select(func.coalesce(func.sum(CreditEntry.amount), 0)).where(
        CreditEntry.telegram_user_id == user_id, CreditEntry.key.like('referral:%')))
    return profile, int(invited or 0), int(earned or 0)
