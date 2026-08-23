"""Telegram uploader script using Pyrogram."""

import asyncio
import os
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import InputMediaDocument

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
CHAT_ID = os.environ.get("CHAT_ID")
ECOSYSTEM = os.environ.get("ECOSYSTEM")


def get_documents():
    """Retrieve APK files and changelog to be uploaded."""
    apk_dir = Path(f"{ECOSYSTEM}/Output")
    documents = []

    for apk in apk_dir.glob("*.apk"):
        documents.append(InputMediaDocument(media=str(apk)))

    if not documents:
        raise FileNotFoundError("No APKs found.")

    changelog_path = Path(f"{ECOSYSTEM}/changelog.md")
    if changelog_path.exists():
        caption = changelog_path.read_text(encoding="utf-8")
    else:
        caption = "Update"

    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    documents[-1].caption = caption
    return documents


def retry(func):
    """Retry decorator for async functions."""
    async def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return await func(*args, **kwargs)
            except (RPCError, ConnectionError, TimeoutError) as exc:
                print(f"Upload failed: {exc}")
                if attempt == 2:
                    raise
    return wrapper


@retry
async def upload_files():
    """Upload documents to Telegram channel."""
    target_chat = CHAT_ID
    if target_chat and target_chat.lstrip('-').isdigit():
        target_chat = int(target_chat)

    documents = get_documents()
    print("Uploading to Telegram...", flush=True)

    async with Client(
        "userbot",
        session_string=SESSION_STRING,
        api_id=API_ID,
        api_hash=API_HASH
    ) as app:
        if isinstance(target_chat, int):
            print(f"Resolving Peer ID for {target_chat}...", flush=True)
            try:
                # Bypass slow dialog synchronization by fetching the chat directly
                await app.get_chat(target_chat)
            except RPCError as err:
                print(f"Note: Could not fetch chat directly ({err})", flush=True)

        await app.send_media_group(chat_id=target_chat, media=documents)
        print("Upload complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(upload_files())
