"""Telegram uploader script using Kurigram."""

import asyncio
import os
from pathlib import Path

from kurigram import Client
from kurigram.errors import RPCError
from kurigram.types import InputMediaDocument


def get_documents() -> list:
    """Retrieve APK files and changelog to be uploaded."""
    ecosystem = os.environ.get("ECOSYSTEM", "")
    apk_dir = Path(f"{ecosystem}/Output")
    documents = []

    for apk in apk_dir.glob("*.apk"):
        documents.append(InputMediaDocument(media=str(apk)))

    if not documents:
        raise FileNotFoundError("No APKs found.")

    changelog_path = Path(f"{ecosystem}/changelog.md")
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
            except (RPCError, ConnectionError, TimeoutError, ValueError, KeyError) as exc:
                print(f"Upload failed: {exc}", flush=True)
                if attempt == 2:
                    raise
        return None
    return wrapper


@retry
async def upload_files() -> None:
    """Upload documents to Telegram channel."""
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    session_string = os.environ.get("SESSION_STRING")
    bot_token = os.environ.get("BOT_TOKEN")
    chat_env = os.environ.get("CHAT_ID", "")

    if chat_env.lstrip('-').isdigit():
        target_chat = int(chat_env)
    else:
        target_chat = chat_env

    documents = get_documents()
    print("Uploading to Telegram...", flush=True)

    client_kwargs = {
        "name": "bot" if bot_token else "userbot",
        "api_id": api_id,
        "api_hash": api_hash,
    }

    if bot_token:
        client_kwargs["bot_token"] = bot_token
    elif session_string:
        client_kwargs["session_string"] = session_string
    else:
        raise ValueError("Either BOT_TOKEN or SESSION_STRING is required")

    async with Client(**client_kwargs) as app:
        if isinstance(target_chat, str) and target_chat.startswith("http"):
            print("Resolving private invite link...", flush=True)
            try:
                chat = await app.get_chat(target_chat)
                target_chat = chat.id
            except RPCError as err:
                print(f"Failed to resolve invite link: {err}", flush=True)

        print(f"Sending media to: {target_chat}", flush=True)
        await app.send_media_group(chat_id=target_chat, media=documents)
        print("Upload complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(upload_files())
