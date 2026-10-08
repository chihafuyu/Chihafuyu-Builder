"""Telegram uploader script using Kurigram (Pyrogram fork)."""

import asyncio
import os
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InputMediaDocument


MAX_RETRIES = 5
CHUNK_SIZE = 10
CAPTION_LIMIT = 1024


def get_documents(ecosystem: str) -> tuple[list, str]:
    """Retrieves APK files and the changelog caption for upload."""
    apk_dir = Path(f"{ecosystem}/Output")
    if not apk_dir.is_dir():
        raise FileNotFoundError(f"Output directory not found: {apk_dir}")

    documents = [
        InputMediaDocument(media=str(apk))
        for apk in sorted(apk_dir.glob("*.apk"))
    ]
    if not documents:
        raise FileNotFoundError(f"No APKs found in {apk_dir}")

    changelog_path = Path(f"{ecosystem}/changelog.md")
    if changelog_path.exists():
        caption = changelog_path.read_text(encoding="utf-8").strip()
    else:
        caption = "Update"

    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 3] + "..."
    return documents, caption


def _get_client_kwargs() -> dict:
    """Constructs initialization arguments for the Pyrogram Client."""
    bot_token = os.environ.get("BOT_TOKEN")
    session_string = os.environ.get("SESSION_STRING")
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")

    if not api_id or not api_hash:
        raise ValueError("API_ID and API_HASH are required.")
    try:
        api_id_int = int(api_id)
    except ValueError as err:
        raise ValueError(f"API_ID must be numeric, got {api_id!r}") from err

    kwargs = {
        "name": "bot" if bot_token else "userbot",
        "api_id": api_id_int,
        "api_hash": api_hash,
        "max_concurrent_transmissions": 1,
    }

    if bot_token:
        kwargs["bot_token"] = bot_token
    elif session_string:
        kwargs["session_string"] = session_string
    else:
        raise ValueError("Either BOT_TOKEN or SESSION_STRING is required.")
    return kwargs


async def _resolve_target_chat(app: Client) -> int | str:
    """Resolves target chat ID, converting invite links if necessary."""
    chat_env = os.environ.get("CHAT_ID", "")
    if not chat_env:
        raise ValueError("CHAT_ID environment variable is required.")
    target_chat: int | str = (
        int(chat_env) if chat_env.lstrip("-").isdigit() else chat_env
    )

    if isinstance(target_chat, str) and target_chat.startswith("http"):
        print("Resolving private invite link...", flush=True)
        try:
            chat = await app.get_chat(target_chat)
        except RPCError as err:
            raise RuntimeError(
                f"Failed to resolve invite link {target_chat}: {err}"
            ) from err
        target_chat = chat.id
    return target_chat


async def _send_with_retry(
    coro_factory, label: str
) -> None:
    """Runs an async send operation with FloodWait-aware retries.

    Raises RuntimeError when every attempt fails so the CI job exits non-zero.
    """
    for attempt in range(MAX_RETRIES):
        try:
            await coro_factory()
            return
        except FloodWait as exc:
            print(f"[{attempt + 1}] Flood wait {exc.value}s ({label}).", flush=True)
            await asyncio.sleep(exc.value + 2)
        except (OSError, TimeoutError, RPCError) as exc:
            print(f"[{attempt + 1}] {label} error: {exc}. Retrying in 10s.", flush=True)
            await asyncio.sleep(10)
    raise RuntimeError(f"{label} failed after {MAX_RETRIES} attempts.")


async def _send_chunk(
    app: Client, target_chat: int | str, chunk: list, chunk_idx: int
) -> None:
    """Uploads a single chunk of documents.

    Captions are always sent separately by the caller, so no per-item
    caption handling is required here.
    """
    if len(chunk) == 1:
        async def _send_single() -> None:
            await app.send_document(
                chat_id=target_chat,
                document=chunk[0].media,
            )
        action = _send_single
    else:
        async def _send_group() -> None:
            await app.send_media_group(chat_id=target_chat, media=chunk)
        action = _send_group

    await _send_with_retry(action, f"chunk {chunk_idx}")
    print(f"Uploaded chunk {chunk_idx}.", flush=True)
    await asyncio.sleep(3)


async def _send_caption(
    app: Client, target_chat: int | str, caption: str
) -> None:
    """Sends the changelog as a standalone message after the media group."""
    async def _send() -> None:
        await app.send_message(chat_id=target_chat, text=caption)

    await _send_with_retry(_send, "caption")
    print("Caption delivered.", flush=True)


async def upload_files() -> None:
    """Uploads APKs and the changelog to the configured Telegram chat."""
    ecosystem = os.environ.get("ECOSYSTEM", "")
    if not ecosystem:
        raise ValueError("ECOSYSTEM environment variable is required.")

    documents, caption = get_documents(ecosystem)
    print(f"Preparing {len(documents)} file(s) for Telegram...", flush=True)

    async with Client(**_get_client_kwargs()) as app:
        target_chat = await _resolve_target_chat(app)
        print(f"Sending media to: {target_chat}", flush=True)

        for i in range(0, len(documents), CHUNK_SIZE):
            chunk = documents[i:i + CHUNK_SIZE]
            await _send_chunk(app, target_chat, chunk, (i // CHUNK_SIZE) + 1)

        await _send_caption(app, target_chat, caption)

    print("Upload complete!", flush=True)


if __name__ == "__main__":
    asyncio.run(upload_files())
