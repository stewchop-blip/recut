"""List pending reservations or explicitly refund one after stopping its worker.

python -m app.maintenance.generations
python -m app.maintenance.generations --refund REQUEST_ID
"""
import argparse
import asyncio
from sqlalchemy import select

from app.database.models import GenerationRun
from app.database.session import db_manager
from app.services.generations import refund_unfinished


async def main(request_id=None):
    db_manager.initialize()
    try:
        async with db_manager.session() as session:
            if request_id:
                changed = await refund_unfinished(session, request_id)
                print('Refunded' if changed else 'No pending reservation')
            else:
                rows = await session.scalars(select(GenerationRun).where(
                    GenerationRun.status == 'reserved').order_by(GenerationRun.created_at).limit(100))
                for row in rows:
                    print(row.request_id, row.telegram_user_id, row.created_at, row.uses_credit)
    finally:
        await db_manager.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refund', help='Confirm the render worker has stopped before refunding')
    asyncio.run(main(parser.parse_args().refund))
