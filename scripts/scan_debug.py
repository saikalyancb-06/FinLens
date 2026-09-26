import os, asyncio
os.environ['LOG_LEVEL'] = 'DEBUG'
from app.database.session import SessionLocal
from app.email.email_scanner import email_scanner_engine
from app.email.models import ConnectedAccount

async def main():
    db = SessionLocal()
    # pick first active connected account
    account = db.query(ConnectedAccount).filter(ConnectedAccount.is_active == True).first()
    if not account:
        print('No active connected account found')
        return
    print('Using account', account.id, account.email_address)
    user_id = str(account.user_id)
    result = await email_scanner_engine.scanInbox(db, user_id)
    print('RESULT', result)

asyncio.run(main())
