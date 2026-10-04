"""
Generates a Telegram session string securely using Kurigram (Pyrogram fork).
"""

import asyncio
import sys

from pyrogram import Client

if sys.platform == "win32":
    POLICY = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if POLICY is not None:
        asyncio.set_event_loop_policy(POLICY())


async def main(api_id: int, api_hash: str) -> None:
    """
    Main asynchronous function to authenticate and export the session string.
    """
    async with Client("my_account", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
        print("\n\n====================================================")
        print("YOUR SESSION STRING (KEEP IT SECRET!):")
        print(await app.export_session_string())
        print("====================================================\n\n")


def run_generator() -> None:
    """Executes the session generator flow."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    print("====================================================")
    print("Telegram Session Generator")
    print("====================================================\n")

    try:
        api_id = int(input("Enter your Telegram API_ID: "))
        api_hash = input("Enter your Telegram API_HASH: ")
        loop.run_until_complete(main(api_id, api_hash))
    except ValueError:
        print("\n[ERROR] API_ID must be an integer!")
        sys.exit(1)
    except Exception as err:  # pylint: disable=broad-exception-caught
        print(f"\n[ERROR] Login failed: {err}")
        sys.exit(1)
    finally:
        input("\nPress Enter to exit...")


if __name__ == "__main__":
    run_generator()
