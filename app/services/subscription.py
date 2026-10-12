"""First-result Telegram channel subscription gate."""
from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.models import Job, JobStatus
from app.database.session import db_manager
from app.core.logging import get_logger

logger = get_logger(__name__)


async def successful_processed_video_count(user_id: int) -> int:
    """Count processed-result jobs that were successfully delivered."""
    async with db_manager.session() as session:
        count = await session.scalar(select(func.count(Job.id)).where(
            Job.telegram_user_id == user_id,
            Job.status == JobStatus.COMPLETED,
            Job.clips_generated > 0,
        ))
    return int(count or 0)


async def has_successful_processed_video(user_id: int) -> bool:
    """True once at least one processed result was successfully delivered."""
    return bool(await successful_processed_video_count(user_id))


async def subscription_required(bot, user_id: int) -> bool:
    """Whether this user must join the channel before sending another video."""
    settings = get_settings()
    if not settings.subscription_gate_enabled or not await has_successful_processed_video(user_id):
        return False
    try:
        member = await bot.get_chat_member(settings.subscription_channel, user_id)
    except Exception as exc:
        # Fail closed: bypassing an unconfigured membership check would defeat
        # the gate. Keep details out of user-facing text and secrets out of logs.
        logger.error("subscription_check_failed", user_id=user_id,
                     channel=settings.subscription_channel,
                     error=type(exc).__name__)
        raise RuntimeError("subscription check unavailable") from exc
    return member.status not in {"creator", "administrator", "member"} and not (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
