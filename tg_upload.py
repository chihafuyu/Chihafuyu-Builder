"""Telegram uploader script using Kurigram (Pyrogram fork)."""

import asyncio
import os
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InputMediaDocument


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
        "max_concurrent_transmissions": 1,
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

        max_retries = 5
        for attempt in range(max_retries):
            try:
                await app.send_media_group(chat_id=target_chat, media=documents)
                print("Upload complete!", flush=True)
                return
            except FloodWait as exc:
                print(f"[{attempt + 1}] Flood wait for {exc.value} seconds.", flush=True)
                await asyncio.sleep(exc.value + 2)
            except (OSError, TimeoutError, RPCError) as exc:
                print(f"[{attempt + 1}] Net/API error: {exc}. Retrying in 10s...", flush=True)
                await asyncio.sleep(10)

        print(f"[FATAL] Failed to upload after {max_retries} attempts.", flush=True)


if __name__ == "__main__":
    asyncio.run(upload_files())
