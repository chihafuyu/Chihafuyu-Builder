"""
Generates a Telegram session string securely using Pyrogram.
"""

import asyncio
import sys
from pyrogram import Client

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

API_ID = int(input("Enter your Telegram API_ID: "))
API_HASH = input("Enter your Telegram API_HASH: ")


async def main():
    """
    Main asynchronous function to authenticate and export the session string.
    """
    async with Client("my_account", api_id=API_ID, api_hash=API_HASH, in_memory=True) as app:
        print("\n\n====================================================")
        print("YOUR SESSION STRING (KEEP IT SECRET!):")
        print(await app.export_session_string())
        print("====================================================\n\n")


if __name__ == "__main__":
    loop.run_until_complete(main())
