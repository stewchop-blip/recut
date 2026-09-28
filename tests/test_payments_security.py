from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import base64

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database.models import Base
from app.services.payments import (
    PaymentRejected, accept_terms, balance, check_checkout, create_order,
    record_payment, record_refund, sales_available,
)


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/payments.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def config():
    return SimpleNamespace(payments_enabled=True, payment_price_stars=10, payment_credits=3,
                           payment_terms="Test terms", payment_support="test",
                           allowed_user_id_set={42})


async def invoice(sessions, config):
    async with sessions.begin() as s:
        order = await create_order(s, 42, config)
    async with sessions.begin() as s:
        await accept_terms(s, order.id, 42, config)
    return order.id


async def test_disabled_cannot_create_or_checkout(sessions, config):
    oid = await invoice(sessions, config)
    config.payments_enabled = False
    assert not sales_available(config)
    async with sessions.begin() as s:
        with pytest.raises(PaymentRejected):
            await create_order(s, 42, config)
        with pytest.raises(PaymentRejected):
            await check_checkout(s, oid, 42, "XTR", 10, config)


@pytest.mark.parametrize("uid,currency,amount", [(43,"XTR",10),(42,"USD",10),(42,"XTR",1)])
async def test_forged_receipt_rejected(sessions, config, uid, currency, amount):
    oid = await invoice(sessions, config)
    async with sessions.begin() as s:
        with pytest.raises(PaymentRejected):
            await record_payment(s, oid, uid, currency, amount, "charge")
        assert await balance(s, 42) == 0


async def test_payment_survives_disable_duplicates_and_refund(sessions, config):
    oid = await invoice(sessions, config)
    config.payments_enabled = False
    async with sessions.begin() as s:
        assert await record_payment(s, oid, 42, "XTR", 10, "charge")
    async with sessions.begin() as s:
        assert not await record_payment(s, oid, 42, "XTR", 10, "charge")
        assert await balance(s, 42) == 3
        assert await record_refund(s, oid, "XTR", 10, "charge")
    async with sessions.begin() as s:
        assert not await record_refund(s, oid, "XTR", 10, "charge")
        assert not await record_payment(s, oid, 42, "XTR", 10, "charge")
        assert await balance(s, 42) == 0


async def test_refund_before_payment_does_not_credit(sessions, config):
    oid = await invoice(sessions, config)
    async with sessions.begin() as s:
        await record_refund(s, oid, "XTR", 10, "charge")
    async with sessions.begin() as s:
        assert not await record_payment(s, oid, 42, "XTR", 10, "charge")
        assert await balance(s, 42) == 0


async def test_parallel_duplicate_credits_once(sessions, config):
    oid = await invoice(sessions, config)
    async def pay():
        async with sessions.begin() as s:
            return await record_payment(s, oid, 42, "XTR", 10, "charge")
    assert sum(await asyncio.gather(pay(), pay())) == 1
    async with sessions.begin() as s:
        assert await balance(s, 42) == 3


async def test_terms_and_checkout_do_not_credit(sessions, config):
    oid = await invoice(sessions, config)
    async with sessions.begin() as s:
        await check_checkout(s, oid, 42, "XTR", 10, config)
        assert await balance(s, 42) == 0
        with pytest.raises(PaymentRejected):
            await accept_terms(s, oid, 42, config)


@pytest.mark.parametrize("url", ["https://tiktok.com.evil.test/x", "https://u:p@tiktok.com/x",
    "https://tiktok.com:8080/x", "file://tiktok.com/x", "http://tiktok.com/x", "https://127.0.0.1/x"])
def test_url_security(url):
    from app.services.downloader.url_utils import is_supported_url
    assert not is_supported_url(url)


async def test_cookie_removed_on_metadata_failure(tmp_path, monkeypatch):
    from app.pipeline.url_downloader import DownloaderService, URLDownloadError
    monkeypatch.setenv("TIKTOK_COOKIES_B64", base64.b64encode(b"test-cookie").decode())
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    service = DownloaderService()
    service._run_ytdlp = AsyncMock(side_effect=URLDownloadError("bad metadata"))
    with pytest.raises(URLDownloadError):
        await service.download("https://vt.tiktok.com/test", tmp_path / "job")
    assert not list(tmp_path.glob("*cookies*"))


async def test_actual_download_size_enforced(tmp_path):
    from app.pipeline.url_downloader import DownloaderService, DownloadTooLargeError
    service = DownloaderService()
    async def run(args, **kwargs):
        if "--dump-json" in args:
            return [{"duration": 1}]
        (tmp_path / "download.mp4").write_bytes(b"x" * (1024*1024+1))
        return []
    service._run_ytdlp = run
    with pytest.raises(DownloadTooLargeError):
        await service.download("https://www.tiktok.com/test", tmp_path, max_size_mb=1)
    assert not (tmp_path / "download.mp4").exists()


async def test_queue_isolates_users_and_retains_cancelled_waiter():
    from app.services.downloader.jobs import MediaJobQueue
    queue = MediaJobQueue(max_heavy=2)
    release = asyncio.Event()
    async def work(value):
        await release.wait()
        return value
    a = asyncio.create_task(queue.run_download(1, "same", lambda: work("a")))
    b = asyncio.create_task(queue.run_download(2, "same", lambda: work("b")))
    await asyncio.sleep(0)
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    again = asyncio.create_task(queue.run_download(1, "same", lambda: work("wrong")))
    await asyncio.sleep(0)
    release.set()
    assert await again == "a"
    assert await b == "b"


def test_nested_log_secrets_redacted():
    from app.core.logging import mask_secrets_processor
    from app.core.config import get_settings
    secret = get_settings().telegram_bot_token
    result = mask_secrets_processor(None, "error", {"error": f"request {secret}",
        "nested": [{"value": secret}], "cookies": "private"})
    assert secret not in str(result)
    assert "private" not in str(result)


async def test_checkout_handler_rejects_when_disabled(monkeypatch):
    from app.bot.handlers import payments
    monkeypatch.setattr(payments, "check_checkout", AsyncMock(side_effect=PaymentRejected()))
    query = SimpleNamespace(invoice_payload="x", from_user=SimpleNamespace(id=42),
                            currency="XTR", total_amount=10, answer=AsyncMock())
    await payments.pre_checkout(query)
    assert query.answer.call_args.kwargs["ok"] is False


async def test_cancel_does_not_delete_other_users_folder(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from app.bot.handlers import start
    from app.core import config
    from app.database.repositories import JobRepository
    @asynccontextmanager
    async def session():
        yield object()
    monkeypatch.setattr(start.db_manager, "session", session)
    monkeypatch.setattr(JobRepository, "cancel_active_jobs", AsyncMock(return_value=(1, [1])))
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(temp_dir=str(tmp_path)))
    own = tmp_path / "job_1_abcd"
    other = tmp_path / "job_11_abcd"
    own.mkdir()
    other.mkdir()
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await start.cmd_cancel(message)
    assert not own.exists()
    assert other.exists()


async def test_receipts_and_support_bypass_revoked_access(monkeypatch):
    from datetime import datetime, timezone
    from aiogram.types import Message, Chat, User, SuccessfulPayment
    from app.bot.middlewares import access
    monkeypatch.setattr(access, "get_settings", lambda: SimpleNamespace(allowed_user_id_set=set()))
    receipt = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="test"),
        successful_payment=SuccessfulPayment(currency="XTR", total_amount=10, invoice_payload="order",
            telegram_payment_charge_id="charge", provider_payment_charge_id=""))
    handler = AsyncMock()
    await access.AccessMiddleware()(handler, receipt, {})
    handler.assert_awaited_once()
    handler.reset_mock()
    await access.AccessMiddleware()(handler, receipt.model_copy(update={"successful_payment": None,
                                                                          "text": "/paysupport"}), {})
    handler.assert_awaited_once()


async def test_duplicate_charge_on_different_order_rolls_back(sessions, config):
    from sqlalchemy.exc import IntegrityError
    first = await invoice(sessions, config)
    second = await invoice(sessions, config)
    async with sessions.begin() as s:
        await record_payment(s, first, 42, "XTR", 10, "same-charge")
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            await record_payment(s, second, 42, "XTR", 10, "same-charge")
    async with sessions.begin() as s:
        assert await balance(s, 42) == 3


async def test_wrong_refund_cannot_remove_credits(sessions, config):
    oid = await invoice(sessions, config)
    async with sessions.begin() as s:
        await record_payment(s, oid, 42, "XTR", 10, "charge")
    async with sessions.begin() as s:
        with pytest.raises(PaymentRejected):
            await record_refund(s, oid, "XTR", 10, "wrong-charge")
        assert await balance(s, 42) == 3
