"""
Telegram platform adapter.

Uses python-telegram-bot library for:
- Receiving messages from users/groups
- Sending responses back
- Handling media and commands
"""

import asyncio
import logging
import os
import re
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

try:
    from telegram import Update, Bot, Message
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
    )
    from telegram.constants import ParseMode, ChatType
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any
    Bot = Any
    Message = Any
    Application = Any
    CommandHandler = Any
    TelegramMessageHandler = Any
    filters = None
    ParseMode = None
    ChatType = None

    # Mock ContextTypes so type annotations using ContextTypes.DEFAULT_TYPE
    # don't crash during class definition when the library isn't installed.
    class _MockContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _MockContextTypes

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.channels.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    SUPPORTED_DOCUMENT_TYPES,
)


def check_telegram_requirements() -> bool:
    """Check if Telegram dependencies are available."""
    return TELEGRAM_AVAILABLE


# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


def _strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes to produce clean plain text.

    Also removes MarkdownV2 bold markers (*text* -> text) so the fallback
    doesn't show stray asterisks from header/bold conversion.
    """
    # Remove escape backslashes before special characters
    cleaned = re.sub(r'\\([_*\[\]()~`>#\+\-=|{}.!\\])', r'\1', text)
    # Remove MarkdownV2 bold markers that format_message converted from **bold**
    cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
    # Remove MarkdownV2 italic markers that format_message converted from *italic*
    # Use word boundary (\b) to avoid breaking snake_case like my_variable_name
    cleaned = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', cleaned)
    return cleaned


class TelegramAdapter(BasePlatformAdapter):
    """
    Telegram bot adapter.
    
    Handles:
    - Receiving messages from users and groups
    - Sending responses with Telegram markdown
    - Forum topics (thread_id support)
    - Media messages
    """
    
    # Telegram message limits
    MAX_MESSAGE_LENGTH = 4096
    
    def __init__(self, config: PlatformConfig, *, agent_id=None, credential_label=None):
        super().__init__(config, Platform.TELEGRAM, agent_id=agent_id, credential_label=credential_label)
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
    
    async def connect(self) -> bool:
        """Connect to Telegram and start polling for updates."""
        if not TELEGRAM_AVAILABLE:
            logger.error(
                "[%s] python-telegram-bot not installed. Run: pip install python-telegram-bot",
                self.name,
            )
            return False
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False
        
        try:
            # Build the application
            self._app = Application.builder().token(self.config.token).build()
            self._bot = self._app.bot
            
            # Register handlers. LOG-29: specific CommandHandlers come
            # FIRST so Telegram routes /help, /status, /stop, /new,
            # /reset, /model to them instead of falling through to the
            # catch-all filters.COMMAND handler (which dispatches the raw
            # text to the LLM — fine as a fallback, bad as the default
            # for commands the user expects to have local semantics).
            self._app.add_handler(CommandHandler("help",   self._cmd_help))
            self._app.add_handler(CommandHandler("status", self._cmd_status))
            self._app.add_handler(CommandHandler("stop",   self._cmd_stop))
            self._app.add_handler(CommandHandler("new",    self._cmd_new))
            self._app.add_handler(CommandHandler("reset",  self._cmd_reset))
            self._app.add_handler(CommandHandler("model",  self._cmd_model))
            self._app.add_handler(TelegramMessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.COMMAND,
                self._handle_command
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                self._handle_location_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                self._handle_media_message
            ))
            
            # Start polling in background
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                # Deliberate trade-off: discard messages queued while the gateway
                # was down rather than processing a backlog storm on restart.
                # Users who messaged during downtime will see no response; they
                # must resend. Change to False if message durability matters more
                # than avoiding backlog amplification.
                drop_pending_updates=True,
            )
            
            # Register bot commands so Telegram shows a hint menu when users type /.
            # LOG-43: trimmed menu — dropped the hermes-specific /update and
            # /reload_mcp (Logos manages these server-side, not via chat) and
            # /provider (cloud routing is admin-only in Logos, not exposed
            # per-session). /personality also dropped — Logos uses souls,
            # configured on the agent record rather than swapped mid-chat.
            try:
                from telegram import BotCommand
                await self._bot.set_my_commands([
                    BotCommand("help",     "Show available commands"),
                    BotCommand("new",      "Start a new conversation"),
                    BotCommand("reset",    "Reset conversation history"),
                    BotCommand("status",   "Show session info"),
                    BotCommand("stop",     "Stop the running agent"),
                    BotCommand("model",    "Show the current model"),
                    BotCommand("reasoning","Show or change reasoning effort"),
                    BotCommand("retry",    "Retry your last message"),
                    BotCommand("undo",     "Remove the last exchange"),
                    BotCommand("sethome",  "Set this chat as the home channel"),
                    BotCommand("compress", "Compress conversation context"),
                    BotCommand("title",    "Set or show the session title"),
                    BotCommand("resume",   "Resume a previously-named session"),
                    BotCommand("usage",    "Show token usage for this session"),
                    BotCommand("insights", "Show usage insights and analytics"),
                    BotCommand("voice",    "Toggle voice reply mode"),
                ])
            except Exception as e:
                logger.warning(
                    "[%s] Could not register Telegram command menu: %s",
                    self.name,
                    e,
                    exc_info=True,
                )
            
            self._running = True
            logger.info("[%s] Connected and polling for Telegram updates", self.name)
            return True
            
        except Exception as e:
            logger.error("[%s] Failed to connect to Telegram: %s", self.name, e, exc_info=True)
            return False
    
    async def disconnect(self) -> None:
        """Stop polling and disconnect."""
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("[%s] Error during Telegram disconnect: %s", self.name, e, exc_info=True)
        
        self._running = False
        self._app = None
        self._bot = None
        logger.info("[%s] Disconnected from Telegram", self.name)
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message to a Telegram chat."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            # Format and split message if needed
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)
            
            message_ids = []
            thread_id = metadata.get("thread_id") if metadata else None
            
            for i, chunk in enumerate(chunks):
                # Try Markdown first, fall back to plain text if it fails
                try:
                    msg = await self._bot.send_message(
                        chat_id=int(chat_id),
                        text=chunk,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_to_message_id=int(reply_to) if reply_to and i == 0 else None,
                        message_thread_id=int(thread_id) if thread_id else None,
                    )
                except Exception as md_error:
                    # Markdown parsing failed, try plain text
                    if "parse" in str(md_error).lower() or "markdown" in str(md_error).lower():
                        logger.warning("[%s] MarkdownV2 parse failed, falling back to plain text: %s", self.name, md_error)
                        # Strip MDV2 escape backslashes so the user doesn't
                        # see raw backslashes littered through the message.
                        plain_chunk = _strip_mdv2(chunk)
                        msg = await self._bot.send_message(
                            chat_id=int(chat_id),
                            text=plain_chunk,
                            parse_mode=None,  # Plain text
                            reply_to_message_id=int(reply_to) if reply_to and i == 0 else None,
                            message_thread_id=int(thread_id) if thread_id else None,
                        )
                    else:
                        raise  # Re-raise if not a parse error
                message_ids.append(str(msg.message_id))
            
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids}
            )
            
        except Exception as e:
            logger.error("[%s] Failed to send Telegram message: %s", self.name, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent Telegram message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            formatted = self.format_message(content)
            try:
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=formatted,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                # Fallback: retry without markdown formatting
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=content,
                )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error(
                "[%s] Failed to edit Telegram message %s: %s",
                self.name,
                message_id,
                e,
                exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a native Telegram voice message or audio file."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            import os
            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")
            
            with open(audio_path, "rb") as audio_file:
                # .ogg files -> send as voice (round playable bubble)
                if audio_path.endswith(".ogg") or audio_path.endswith(".opus"):
                    _voice_thread = metadata.get("thread_id") if metadata else None
                    msg = await self._bot.send_voice(
                        chat_id=int(chat_id),
                        voice=audio_file,
                        caption=caption[:1024] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=int(_voice_thread) if _voice_thread else None,
                    )
                else:
                    # .mp3 and others -> send as audio file
                    _audio_thread = metadata.get("thread_id") if metadata else None
                    msg = await self._bot.send_audio(
                        chat_id=int(chat_id),
                        audio=audio_file,
                        caption=caption[:1024] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=int(_audio_thread) if _audio_thread else None,
                    )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram voice/audio, falling back to base adapter: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_voice(chat_id, audio_path, caption, reply_to)
    
    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively as a Telegram photo."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            import os
            if not os.path.exists(image_path):
                return SendResult(success=False, error=f"Image file not found: {image_path}")
            
            with open(image_path, "rb") as image_file:
                msg = await self._bot.send_photo(
                    chat_id=int(chat_id),
                    photo=image_file,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram local image, falling back to base adapter: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image_file(chat_id, image_path, caption, reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file natively as a Telegram file attachment."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            display_name = file_name or os.path.basename(file_path)

            with open(file_path, "rb") as f:
                msg = await self._bot.send_document(
                    chat_id=int(chat_id),
                    document=f,
                    filename=display_name,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            print(f"[{self.name}] Failed to send document: {e}")
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively as a Telegram video message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(video_path):
                return SendResult(success=False, error=f"Video file not found: {video_path}")

            with open(video_path, "rb") as f:
                msg = await self._bot.send_video(
                    chat_id=int(chat_id),
                    video=f,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            print(f"[{self.name}] Failed to send video: {e}")
            return await super().send_video(chat_id, video_path, caption, reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Telegram photo.
        
        Tries URL-based send first (fast, works for <5MB images).
        Falls back to downloading and uploading as file (supports up to 10MB).
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            # Telegram can send photos directly from URLs (up to ~5MB)
            _photo_thread = metadata.get("thread_id") if metadata else None
            msg = await self._bot.send_photo(
                chat_id=int(chat_id),
                photo=image_url,
                caption=caption[:1024] if caption else None,  # Telegram caption limit
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=int(_photo_thread) if _photo_thread else None,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[%s] URL-based send_photo failed, trying file upload: %s",
                self.name,
                e,
                exc_info=True,
            )
            # Fallback: download and upload as file (supports up to 10MB)
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    image_data = resp.content
                
                msg = await self._bot.send_photo(
                    chat_id=int(chat_id),
                    photo=image_data,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                )
                return SendResult(success=True, message_id=str(msg.message_id))
            except Exception as e2:
                logger.error(
                    "[%s] File upload send_photo also failed: %s",
                    self.name,
                    e2,
                    exc_info=True,
                )
                # Final fallback: send URL as text
                return await super().send_image(chat_id, image_url, caption, reply_to)
    
    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an animated GIF natively as a Telegram animation (auto-plays inline)."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            _anim_thread = metadata.get("thread_id") if metadata else None
            msg = await self._bot.send_animation(
                chat_id=int(chat_id),
                animation=animation_url,
                caption=caption[:1024] if caption else None,
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=int(_anim_thread) if _anim_thread else None,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram animation, falling back to photo: %s",
                self.name,
                e,
                exc_info=True,
            )
            # Fallback: try as a regular photo
            return await self.send_image(chat_id, animation_url, caption, reply_to)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Send typing indicator."""
        if self._bot:
            try:
                _typing_thread = metadata.get("thread_id") if metadata else None
                await self._bot.send_chat_action(
                    chat_id=int(chat_id),
                    action="typing",
                    message_thread_id=int(_typing_thread) if _typing_thread else None,
                )
            except Exception as e:
                # Typing failures are non-fatal; log at debug level only.
                logger.debug(
                    "[%s] Failed to send Telegram typing indicator: %s",
                    self.name,
                    e,
                    exc_info=True,
                )
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Telegram chat."""
        if not self._bot:
            return {"name": "Unknown", "type": "dm"}
        
        try:
            chat = await self._bot.get_chat(int(chat_id))
            
            chat_type = "dm"
            if chat.type == ChatType.GROUP:
                chat_type = "group"
            elif chat.type == ChatType.SUPERGROUP:
                chat_type = "group"
                if chat.is_forum:
                    chat_type = "forum"
            elif chat.type == ChatType.CHANNEL:
                chat_type = "channel"
            
            return {
                "name": chat.title or chat.full_name or str(chat_id),
                "type": chat_type,
                "username": chat.username,
                "is_forum": getattr(chat, "is_forum", False),
            }
        except Exception as e:
            logger.error(
                "[%s] Failed to get Telegram chat info for %s: %s",
                self.name,
                chat_id,
                e,
                exc_info=True,
            )
            return {"name": str(chat_id), "type": "dm", "error": str(e)}
    
    def format_message(self, content: str) -> str:
        """
        Convert standard markdown to Telegram MarkdownV2 format.

        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to MarkdownV2 syntax,
        and all remaining special characters are escaped.
        """
        if not content:
            return content

        placeholders: dict = {}
        counter = [0]

        def _ph(value: str) -> str:
            """Stash *value* behind a placeholder token that survives escaping."""
            key = f"\x00PH{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # 1) Protect fenced code blocks (``` ... ```)
        text = re.sub(
            r'(```(?:[^\n]*\n)?[\s\S]*?```)',
            lambda m: _ph(m.group(0)),
            text,
        )

        # 2) Protect inline code (`...`)
        text = re.sub(r'(`[^`]+`)', lambda m: _ph(m.group(0)), text)

        # 3) Convert markdown links – escape the display text; inside the URL
        #    only ')' and '\' need escaping per the MarkdownV2 spec.
        def _convert_link(m):
            display = _escape_mdv2(m.group(1))
            url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
            return _ph(f'[{display}]({url})')

        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _convert_link, text)

        # 4) Convert markdown headers (## Title) → bold *Title*
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers that may appear inside a header
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{_escape_mdv2(inner)}*')

        text = re.sub(
            r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
        )

        # 5) Convert bold: **text** → *text* (MarkdownV2 bold)
        text = re.sub(
            r'\*\*(.+?)\*\*',
            lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
            text,
        )

        # 6) Convert italic: *text* (single asterisk) → _text_ (MarkdownV2 italic)
        #    [^*\n]+ prevents matching across newlines (which would corrupt
        #    bullet lists using * markers and multi-line content).
        text = re.sub(
            r'\*([^*\n]+)\*',
            lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
            text,
        )

        # 7) Escape remaining special characters in plain text
        text = _escape_mdv2(text)

        # 8) Restore placeholders in reverse insertion order so that
        #    nested references (a placeholder inside another) resolve correctly.
        for key in reversed(list(placeholders.keys())):
            text = text.replace(key, placeholders[key])

        return text
    
    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages."""
        if not update.message or not update.message.text:
            return
        
        event = self._build_message_event(update.message, MessageType.TEXT)
        await self.handle_message(event)
    
    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Catch-all for commands NOT handled by a specific CommandHandler
        above. Falls through to the LLM so the agent can respond
        conversationally (or surface "I don't know that command")."""
        if not update.message or not update.message.text:
            return

        event = self._build_message_event(update.message, MessageType.COMMAND)
        await self.handle_message(event)

    # ── LOG-29: specific slash-command handlers ────────────────────────
    # Each reaches into gateway state directly so the user gets a local,
    # deterministic response instead of bouncing through the LLM for
    # every command. Helpers keep error paths quiet — a command failing
    # should not kill the poll loop.

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/help` — list what the bot actually responds to locally."""
        if not update.message: return
        text = (
            "*Available commands*\n\n"
            "/new — start a fresh conversation\n"
            "/reset — reset the current session's history\n"
            "/status — show session info (model, messages, tokens)\n"
            "/stop — cancel the in-flight reply\n"
            "/model — show the model this agent is using\n"
            "/help — this list\n\n"
            "Anything else you send goes to the agent. No command? "
            "Just type your message."
        )
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.debug("[%s] /help reply failed: %s", self.name, e)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/status` — snapshot of the bound agent + session."""
        if not update.message: return
        agent = self.config.extra.get("agent_name") or self.name
        lines = [f"*Status*", f"Agent: `{agent}`"]
        try:
            from gateway.auth import db as _auth_db
            row = _auth_db.get_agent_by_name(agent) if agent else None
            if row:
                lines.append(f"Model: `{row.get('model') or 'default'}`")
                if row.get("soul_slug"):
                    lines.append(f"Soul:  `{row['soul_slug']}`")
        except Exception as e:
            logger.debug("[%s] /status agent lookup failed: %s", self.name, e)
        try:
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.debug("[%s] /status reply failed: %s", self.name, e)

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/stop` — interrupt the running dispatch for this chat by
        setting the per-session interrupt event. Same mechanism a new
        incoming message uses, minus re-dispatch."""
        if not update.message: return
        event = self._build_message_event(update.message, MessageType.COMMAND)
        session_key = ""
        try:
            from gateway.channels.base import build_session_key
            session_key = build_session_key(event.source)
        except Exception:
            pass
        interrupted = False
        try:
            if session_key and session_key in self._active_sessions:
                self._active_sessions[session_key].set()
                interrupted = True
        except Exception as e:
            logger.debug("[%s] /stop signal failed: %s", self.name, e)
        msg = "\u23f9\ufe0f Stopped." if interrupted else "Nothing running."
        try:
            await update.message.reply_text(msg)
        except Exception as e:
            logger.debug("[%s] /stop reply failed: %s", self.name, e)

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/new` — start a fresh session by rotating the session key.
        Delegates to the base adapter's session_store if wired; otherwise
        falls through so the next message naturally creates a new session
        once the existing one expires."""
        if not update.message: return
        event = self._build_message_event(update.message, MessageType.COMMAND)
        ok = await self._reset_session(event, hard=True)
        txt = "\u2728 New conversation." if ok else (
            "I'll start fresh on your next message."
        )
        try:
            await update.message.reply_text(txt)
        except Exception as e:
            logger.debug("[%s] /new reply failed: %s", self.name, e)

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/reset` — clear the current session's transcript without
        rotating the session key. Keeps the chat bound to the same
        agent incarnation; just drops context."""
        if not update.message: return
        event = self._build_message_event(update.message, MessageType.COMMAND)
        ok = await self._reset_session(event, hard=False)
        txt = "\u21bb Context cleared." if ok else (
            "Couldn't find an active session to reset."
        )
        try:
            await update.message.reply_text(txt)
        except Exception as e:
            logger.debug("[%s] /reset reply failed: %s", self.name, e)

    async def _cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """`/model` — show the agent's currently bound model. Read-only:
        changing a model is an admin operation and lives in the web UI."""
        if not update.message: return
        agent = self.config.extra.get("agent_name") or self.name
        model = "unknown"
        try:
            from gateway.auth import db as _auth_db
            row = _auth_db.get_agent_by_name(agent) if agent else None
            if row and row.get("model"):
                model = row["model"]
        except Exception:
            pass
        try:
            await update.message.reply_text(
                f"*Model:* `{model}`\n\nChange this from the Logos admin UI.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.debug("[%s] /model reply failed: %s", self.name, e)

    async def _reset_session(self, event, *, hard: bool) -> bool:
        """Session reset helper. `hard=True` rotates the session key (so
        the next message lands in a brand-new session); `hard=False`
        clears the transcript but keeps the key. Returns True if we
        actually found a session to reset."""
        try:
            from gateway.channels.base import build_session_key
            session_key = build_session_key(event.source)
        except Exception:
            return False
        # Signal interrupt if anything's in flight.
        try:
            if session_key in self._active_sessions:
                self._active_sessions[session_key].set()
        except Exception:
            pass
        # Runtime-optional: if set_message_handler was called from the
        # runner, it may have exposed a session_store on the adapter. If
        # not wired, fall through with False so the UI text reflects that.
        session_store = getattr(self, "_session_store", None) or getattr(
            getattr(self, "_runner", None), "session_store", None,
        )
        if not session_store:
            return False
        try:
            if hard and hasattr(session_store, "reset_session"):
                return bool(session_store.reset_session(session_key))
            if not hard and hasattr(session_store, "clear_transcript"):
                return bool(session_store.clear_transcript(session_key))
        except Exception as e:
            logger.debug("[%s] _reset_session failed: %s", self.name, e)
        return False
    
    async def _handle_location_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming location/venue pin messages."""
        if not update.message:
            return

        msg = update.message
        venue = getattr(msg, "venue", None)
        location = getattr(venue, "location", None) if venue else getattr(msg, "location", None)

        if not location:
            return

        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return

        # Build a text message with coordinates and context
        parts = ["[The user shared a location pin.]"]
        if venue:
            title = getattr(venue, "title", None)
            address = getattr(venue, "address", None)
            if title:
                parts.append(f"Venue: {title}")
            if address:
                parts.append(f"Address: {address}")
        parts.append(f"latitude: {lat}")
        parts.append(f"longitude: {lon}")
        parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        parts.append("Ask what they'd like to find nearby (restaurants, cafes, etc.) and any preferences.")

        event = self._build_message_event(msg, MessageType.LOCATION)
        event.text = "\n".join(parts)
        await self.handle_message(event)

    async def _handle_media_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming media messages, downloading images to local cache."""
        if not update.message:
            return
        
        msg = update.message
        
        # Determine media type
        if msg.sticker:
            msg_type = MessageType.STICKER
        elif msg.photo:
            msg_type = MessageType.PHOTO
        elif msg.video:
            msg_type = MessageType.VIDEO
        elif msg.audio:
            msg_type = MessageType.AUDIO
        elif msg.voice:
            msg_type = MessageType.VOICE
        elif msg.document:
            msg_type = MessageType.DOCUMENT
        else:
            msg_type = MessageType.DOCUMENT
        
        event = self._build_message_event(msg, msg_type)
        
        # Add caption as text
        if msg.caption:
            event.text = msg.caption
        
        # Handle stickers: describe via vision tool with caching
        if msg.sticker:
            await self._handle_sticker(msg, event)
            await self.handle_message(event)
            return
        
        # Download photo to local image cache so the vision tool can access it
        # even after Telegram's ephemeral file URLs expire (~1 hour).
        if msg.photo:
            try:
                # msg.photo is a list of PhotoSize sorted by size; take the largest
                photo = msg.photo[-1]
                file_obj = await photo.get_file()
                # Download the image bytes directly into memory
                image_bytes = await file_obj.download_as_bytearray()
                # Determine extension from the file path if available
                ext = ".jpg"
                if file_obj.file_path:
                    for candidate in [".png", ".webp", ".gif", ".jpeg", ".jpg"]:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                # Save to cache and populate media_urls with the local path
                cached_path = cache_image_from_bytes(bytes(image_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [f"image/{ext.lstrip('.')}"]
                logger.info("[Telegram] Cached user photo at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache photo: %s", e, exc_info=True)
        
        # Download voice/audio messages to cache for STT transcription
        if msg.voice:
            try:
                file_obj = await msg.voice.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".ogg")
                event.media_urls = [cached_path]
                event.media_types = ["audio/ogg"]
                logger.info("[Telegram] Cached user voice at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache voice: %s", e, exc_info=True)
        elif msg.audio:
            try:
                file_obj = await msg.audio.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".mp3")
                event.media_urls = [cached_path]
                event.media_types = ["audio/mp3"]
                logger.info("[Telegram] Cached user audio at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache audio: %s", e, exc_info=True)

        # Download document files to cache for agent processing
        elif msg.document:
            doc = msg.document
            try:
                # Determine file extension
                ext = ""
                original_filename = doc.file_name or ""
                if original_filename:
                    _, ext = os.path.splitext(original_filename)
                    ext = ext.lower()

                # If no extension from filename, reverse-lookup from MIME type
                if not ext and doc.mime_type:
                    mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                    ext = mime_to_ext.get(doc.mime_type, "")

                # Check if supported
                if ext not in SUPPORTED_DOCUMENT_TYPES:
                    supported_list = ", ".join(sorted(SUPPORTED_DOCUMENT_TYPES.keys()))
                    event.text = (
                        f"Unsupported document type '{ext or 'unknown'}'. "
                        f"Supported types: {supported_list}"
                    )
                    logger.info("[Telegram] Unsupported document type: %s", ext or "unknown")
                    await self.handle_message(event)
                    return

                # Check file size (Telegram Bot API limit: 20 MB)
                MAX_DOC_BYTES = 20 * 1024 * 1024
                if not doc.file_size or doc.file_size > MAX_DOC_BYTES:
                    event.text = (
                        "The document is too large or its size could not be verified. "
                        "Maximum: 20 MB."
                    )
                    logger.info("[Telegram] Document too large: %s bytes", doc.file_size)
                    await self.handle_message(event)
                    return

                # Download and cache
                file_obj = await doc.get_file()
                doc_bytes = await file_obj.download_as_bytearray()
                raw_bytes = bytes(doc_bytes)
                cached_path = cache_document_from_bytes(raw_bytes, original_filename or f"document{ext}")
                mime_type = SUPPORTED_DOCUMENT_TYPES[ext]
                event.media_urls = [cached_path]
                event.media_types = [mime_type]
                logger.info("[Telegram] Cached user document at %s", cached_path)

                # For text files, inject content into event.text (capped at 100 KB)
                MAX_TEXT_INJECT_BYTES = 100 * 1024
                if ext in (".md", ".txt") and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        display_name = original_filename or f"document{ext}"
                        display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        if event.text:
                            event.text = f"{injection}\n\n{event.text}"
                        else:
                            event.text = injection
                    except UnicodeDecodeError:
                        logger.warning(
                            "[Telegram] Could not decode text file as UTF-8, skipping content injection",
                            exc_info=True,
                        )

            except Exception as e:
                logger.warning("[Telegram] Failed to cache document: %s", e, exc_info=True)

        await self.handle_message(event)
    
    async def _handle_sticker(self, msg: Message, event: "MessageEvent") -> None:
        """
        Describe a Telegram sticker via vision analysis, with caching.

        For static stickers (WEBP), we download, analyze with vision, and cache
        the description by file_unique_id. For animated/video stickers, we inject
        a placeholder noting the emoji.
        """
        from gateway.sticker_cache import (
            get_cached_description,
            cache_sticker_description,
            build_sticker_injection,
            build_animated_sticker_injection,
            STICKER_VISION_PROMPT,
        )

        sticker = msg.sticker
        emoji = sticker.emoji or ""
        set_name = sticker.set_name or ""

        # Animated and video stickers can't be analyzed as static images
        if sticker.is_animated or sticker.is_video:
            event.text = build_animated_sticker_injection(emoji)
            return

        # Check the cache first
        cached = get_cached_description(sticker.file_unique_id)
        if cached:
            event.text = build_sticker_injection(
                cached["description"], cached.get("emoji", emoji), cached.get("set_name", set_name)
            )
            logger.info("[Telegram] Sticker cache hit: %s", sticker.file_unique_id)
            return

        # Cache miss -- download and analyze
        try:
            file_obj = await sticker.get_file()
            image_bytes = await file_obj.download_as_bytearray()
            cached_path = cache_image_from_bytes(bytes(image_bytes), ext=".webp")
            logger.info("[Telegram] Analyzing sticker at %s", cached_path)

            from tools.vision_tools import vision_analyze_tool
            import json as _json

            result_json = await vision_analyze_tool(
                image_url=cached_path,
                user_prompt=STICKER_VISION_PROMPT,
            )
            result = _json.loads(result_json)

            if result.get("success"):
                description = result.get("analysis", "a sticker")
                cache_sticker_description(sticker.file_unique_id, description, emoji, set_name)
                event.text = build_sticker_injection(description, emoji, set_name)
            else:
                # Vision failed -- use emoji as fallback
                event.text = build_sticker_injection(
                    f"a sticker with emoji {emoji}" if emoji else "a sticker",
                    emoji, set_name,
                )
        except Exception as e:
            logger.warning("[Telegram] Sticker analysis error: %s", e, exc_info=True)
            event.text = build_sticker_injection(
                f"a sticker with emoji {emoji}" if emoji else "a sticker",
                emoji, set_name,
            )

    def _build_message_event(self, message: Message, msg_type: MessageType) -> MessageEvent:
        """Build a MessageEvent from a Telegram message."""
        chat = message.chat
        user = message.from_user
        
        # Determine chat type
        chat_type = "dm"
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            chat_type = "group"
        elif chat.type == ChatType.CHANNEL:
            chat_type = "channel"
        
        # Build source
        source = self.build_source(
            chat_id=str(chat.id),
            chat_name=chat.title or (chat.full_name if hasattr(chat, "full_name") else None),
            chat_type=chat_type,
            user_id=str(user.id) if user else None,
            user_name=user.full_name if user else None,
            thread_id=str(message.message_thread_id) if message.message_thread_id else None,
        )
        
        return MessageEvent(
            text=message.text or "",
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.message_id),
            timestamp=message.date,
        )
