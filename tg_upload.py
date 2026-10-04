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

    # Attach the caption to the last document
    documents[-1].caption = caption
    return documents


def _get_client_kwargs() -> dict:
    """Constructs initialization arguments for the Pyrogram Client."""
    bot_token = os.environ.get("BOT_TOKEN")
    session_string = os.environ.get("SESSION_STRING")

    kwargs = {
        "name": "bot" if bot_token else "userbot",
        "api_id": os.environ.get("API_ID"),
        "api_hash": os.environ.get("API_HASH"),
        "max_concurrent_transmissions": 1,
    }

    if bot_token:
        kwargs["bot_token"] = bot_token
    elif session_string:
        kwargs["session_string"] = session_string
    else:
        raise ValueError("Either BOT_TOKEN or SESSION_STRING is required")

    return kwargs


async def _resolve_target_chat(app: Client) -> int | str:
    """Resolves target chat ID, converting invite links if necessary."""
    chat_env = os.environ.get("CHAT_ID", "")
    target_chat = int(chat_env) if chat_env.lstrip('-').isdigit() else chat_env

    if isinstance(target_chat, str) and target_chat.startswith("http"):
        print("Resolving private invite link...", flush=True)
        try:
            chat = await app.get_chat(target_chat)
            target_chat = chat.id
        except RPCError as err:
            print(f"Failed to resolve invite link: {err}", flush=True)

    return target_chat


async def _send_chunk(
    app: Client, target_chat: int | str, chunk: list, chunk_idx: int
) -> None:
    """Uploads a single chunk of documents with retry logic."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            if len(chunk) == 1:
                await app.send_document(
                    chat_id=target_chat,
                    document=chunk[0].media,
                    caption=chunk[0].caption
                )
            else:
                await app.send_media_group(chat_id=target_chat, media=chunk)

            print(f"Uploaded chunk {chunk_idx}...", flush=True)
            await asyncio.sleep(3)
            break
        except FloodWait as exc:
            print(f"[{attempt + 1}] Flood wait for {exc.value} seconds.", flush=True)
            await asyncio.sleep(exc.value + 2)
        except (OSError, TimeoutError, RPCError) as exc:
            print(f"[{attempt + 1}] API error: {exc}. Retrying in 10s...", flush=True)
            await asyncio.sleep(10)
    else:
        print(f"[FATAL] Failed to upload chunk {chunk_idx}.", flush=True)


async def upload_files() -> None:
    """Upload documents to Telegram channel with chunking to avoid limits."""
    documents = get_documents()
    print(f"Preparing {len(documents)} file(s) for Telegram...", flush=True)

    async with Client(**_get_client_kwargs()) as app:
        target_chat = await _resolve_target_chat(app)
        print(f"Sending media to: {target_chat}", flush=True)

        chunk_size = 10
        for i in range(0, len(documents), chunk_size):
            chunk = documents[i:i + chunk_size]
            chunk_idx = (i // chunk_size) + 1
            await _send_chunk(app, target_chat, chunk, chunk_idx)

        print("Upload complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(upload_files())
