#!/usr/bin/env python3
"""
Telegram Bridge Command для MeshCore Bot
Пересылает сообщения из MeshCore в Telegram и позволяет отвечать из Telegram обратно в сеть.
+ Автоматически сообщает chat_id при первом сообщении в чате.
+ Команда /status — показывает текущее состояние моста и подключения.
+ Пересылаются ВСЕ сообщения (включая от своей ноды)
+ Добавлен метод should_execute() с логикой фильтрации (как в GreeterCommand)
+ Отправка в Telegram происходит только после should_execute()
+ Исправлена отправка из другого потока через run_coroutine_threadsafe
"""

import asyncio
from typing import Dict, Optional
from concurrent.futures import Future
from .base_command import BaseCommand
from ..models import MeshMessage

# Глобальный словарь: telegram_message_id → исходное MeshMessage (для ответов)
REPLY_MAPPING: Dict[tuple, MeshMessage] = {}
REPLY_MAPPING_LOCK = asyncio.Lock()

# Множество чатов, куда уже отправляли chat_id
CHAT_ID_SENT: set = set()


class TelegramBridgeCommand(BaseCommand):
    name = "telegram_bridge"
    keywords = []
    description = "Мост с Telegram: пересылает сообщения в TG и обратно + авто-отправка chat_id + /status"
    category = "system"
    requires_dm = False

    def __init__(self, bot):
        super().__init__(bot)

        self.enabled = bot.config.getboolean('Telegram_Bridge', 'enabled', fallback=False)
        self.telegram_chat_id = bot.config.get('Telegram_Bridge', 'telegram_chat_id', fallback=None)
        self.telegram_token = bot.config.get('Telegram_Bridge', 'telegram_token', fallback=None)
        self.forward_channels = self._parse_forward_channels()
        self.include_dm = bot.config.getboolean('Telegram_Bridge', 'include_dm', fallback=False)
        self.prefix_sender = bot.config.getboolean('Telegram_Bridge', 'prefix_sender', fallback=True)

        if not self.enabled:
            self.logger.info("Telegram Bridge отключён в конфиге")
            return

        if not self.telegram_token:
            self.logger.warning("Telegram Bridge: не указан telegram_token — мост отключён")
            self.enabled = False
            return

        self.tg_bot = None
        self.tg_loop = None  # Ссылка на event loop Telegram-потока
        self._init_telegram_bot()

        if self.telegram_chat_id:
            self.logger.info(f"Telegram Bridge включён → целевой чат {self.telegram_chat_id}")
        else:
            self.logger.info("Telegram Bridge включён (chat_id не указан — пересылка из MeshCore отключена, только авто-определение chat_id)")

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Глобальный Telegram-мост — получаем все сообщения для проверки should_execute"""
        return False
        
    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        """Bridge doesn't match custom syntax"""
        return False
        
    def get_response_format(self) -> Optional[str]:
        """Get the response format for this command from config"""
        return ""
        
    def _parse_forward_channels(self) -> set:
        raw = self.bot.config.get('Telegram_Bridge', 'forward_channels', fallback='all').strip().lower()
        self.logger.debug(f"Raw forward_channels из config: '{raw}'")
        if raw in ['all', '*', '']:
            self.logger.info("forward_channels = all → пересылаем ВСЕ каналы")
            return set()
        channels = {ch.strip() for ch in raw.split(',') if ch.strip()}
        self.logger.info(f"forward_channels parsed: {channels}")
        return channels

    def should_execute(self, message: MeshMessage) -> bool:
        """
        Проверка, нужно ли пересылать данное сообщение в Telegram.
        Логика аналогична GreeterCommand — все фильтры здесь.
        """
        self.logger.debug(f"[TelegramBridge] should_execute вызван для сообщения от {message.sender_id} (канал: {message.channel}, DM: {message.is_dm})")        
        
        if not self.enabled:
            self.logger.debug("Мост отключён (enabled=False)")
            return False

        if not self.telegram_chat_id:
            self.logger.debug("telegram_chat_id не указан — пересылка отключена")
            return False

        # DM фильтр
        if message.is_dm and not self.include_dm:
            self.logger.debug("Сообщение DM, но include_dm=False — пропуск")
            return False

        # Фильтр по каналам
        if self.forward_channels:  # не пустой set
            if message.channel not in self.forward_channels:
                self.logger.debug(f"Канал '{message.channel}' не в разрешённых {self.forward_channels} — пропуск")
                return False
        # Если forward_channels пустой (all) — пропускаем все

        return True

    def _init_telegram_bot(self):
        """Инициализация AsyncTeleBot с запуском поллинга в отдельном потоке"""
        try:
            from telebot.async_telebot import AsyncTeleBot
            import threading

            self.tg_bot = AsyncTeleBot(self.telegram_token)

            # Команда /status
            @self.tg_bot.message_handler(commands=['status'])
            async def handle_status(tg_message):
                chat_id_str = str(tg_message.chat.id)
                conn_status = "🟢 Подключено" if hasattr(self.bot.meshcore, 'connected') and self.bot.meshcore.connected else "🔴 Отключено"
                bridge_status = "✅ Активен" if self.enabled and self.telegram_chat_id else "⚠️ Ожидает chat_id"
                target_chat = self.telegram_chat_id or "не указан"

                status_text = (
                    f"📡 <b>Статус Telegram Bridge</b>\n\n"
                    f"Мост: {bridge_status}\n"
                    f"MeshCore: {conn_status}\n"
                    f"Целевой чат: <code>{target_chat}</code>\n"
                    f"Текущий чат: <code>{tg_message.chat.id}</code>\n\n"
                    f"Если chat_id не совпадает — укажите правильный в config.ini."
                )
                await self.tg_bot.reply_to(tg_message, status_text, parse_mode='HTML')

            # Обработка всех сообщений из Telegram
            @self.tg_bot.message_handler(func=lambda m: True)
            async def handle_telegram_message(tg_message):
                chat_id_str = str(tg_message.chat.id)

                # Авто-отправка chat_id при первом сообщении
                if tg_message.chat.id not in CHAT_ID_SENT:
                    welcome_text = (
                        f"Привет! Это мост MeshCore ↔ Telegram.\n"
                        f"<b>Chat ID этого чата:</b> <code>{tg_message.chat.id}</code>\n\n"
                        f"Скопируйте и вставьте в config.ini:\n"
                        f"<code>telegram_chat_id = {tg_message.chat.id}</code>\n\n"
                        f"После перезапуска — сообщения из сети придут сюда.\n"
                        f"/status — проверить состояние"
                    )
                    await self.tg_bot.reply_to(tg_message, welcome_text, parse_mode='HTML')
                    CHAT_ID_SENT.add(tg_message.chat.id)
                    self.logger.info(f"Автоматически отправлен chat_id {tg_message.chat.id}")
                    return

                # Ответы из TG в MeshCore (только в целевом чате)
                if (tg_message.reply_to_message and
                    tg_message.reply_to_message.from_user.is_bot and
                    self.telegram_chat_id and
                    chat_id_str == self.telegram_chat_id):

                    async with REPLY_MAPPING_LOCK:
                        key = (tg_message.chat.id, tg_message.reply_to_message.message_id)
                        original_mesh_msg = REPLY_MAPPING.get(key)

                    if original_mesh_msg:
                        response_text = tg_message.text or tg_message.caption or "[медиа/стикер]"
                        sender_name = (tg_message.from_user.full_name or
                                       tg_message.from_user.username or
                                       "TG-User")
                        reply_text = f"Ответ от {sender_name} (TG):\n{response_text}"

                        await self.bot.meshcore.commands.send_text(
                            text=reply_text,
                            channel=original_mesh_msg.channel,
                            reply_id=original_mesh_msg.sender_id if original_mesh_msg.is_dm else None
                        )
                        self.logger.info(f"Ответ из TG отправлен в MeshCore")

            # Запуск поллинга в отдельном потоке
            def run_polling():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.tg_loop = loop  # Сохраняем loop для отправки из execute
                loop.run_until_complete(self.tg_bot.polling(none_stop=True, interval=1, timeout=60))

            threading.Thread(target=run_polling, daemon=True).start()
            self.logger.info("Telegram поллинг запущен в отдельном потоке")

        except Exception as e:
            self.logger.error(f"Ошибка инициализации Telegram бота: {e}")
            self.enabled = False

    async def _send_to_telegram_from_correct_loop(self, chat_id: str, text: str):
        """Отправка сообщения в правильном event loop Telegram-потока"""
        if not self.tg_bot or not self.tg_loop:
            self.logger.error("Telegram бот или loop не инициализированы")
            return None

        future = asyncio.run_coroutine_threadsafe(
            self.tg_bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='HTML',
                disable_web_page_preview=True
            ),
            self.tg_loop
        )

        try:
            return future.result(timeout=30)
        except Exception as e:
            self.logger.error(f"Ошибка при отправке в Telegram: {e}", exc_info=True)
            return None

    async def execute(self, message: MeshMessage) -> bool:
        """Пересылка сообщений из MeshCore → Telegram только после проверки should_execute"""
        self.logger.debug(f"[TelegramBridge] execute вызван для сообщения от {message.sender_id} (канал: {message.channel}, DM: {message.is_dm})")

        # Первая проверка — должен ли бридж обрабатывать это сообщение
        if not self.should_execute(message):
            return False

        self.logger.info(f"Пересылаем сообщение от {message.sender_id} в Telegram")

        try:
            sender = message.sender_id or "Unknown"
            content = message.content.strip()

            # Формируем префикс с отправителем
            text = f"<b>{sender}</b>: {content}" if self.prefix_sender else content

            # Формируем информацию о канале с #
            if message.is_dm:
                # Для DM — # + имя отправителя (без _, -, пробелов)
                clean_sender = sender.replace('_', '').replace('-', '').replace(' ', '')
                channel_tag = f"#{clean_sender}"
                channel_info = f" (#DM {channel_tag})"
            else:
                # Для публичных каналов — добавляем # если его нет, удаляем _ и -
                if message.channel:
                    channel_name = message.channel.strip()
                    clean_channel = channel_name.replace('_', '').replace('-', '').replace(' ', '')
                    if not clean_channel.startswith('#'):
                        clean_channel = f"#{clean_channel}"
                    channel_info = f" {clean_channel}"
                else:
                    channel_info = " #unknown"

            full_text = f"{text}{channel_info}"

            sent_msg = await self._send_to_telegram_from_correct_loop(
                chat_id=self.telegram_chat_id,
                text=full_text
            )

            if not sent_msg:
                self.logger.error("Не удалось отправить сообщение в Telegram")
                return False

            async with REPLY_MAPPING_LOCK:
                key = (sent_msg.chat.id, sent_msg.message_id)
                REPLY_MAPPING[key] = message

                if len(REPLY_MAPPING) > 1000:
                    keys_to_remove = sorted(REPLY_MAPPING.keys(), key=lambda k: k[1])[:200]
                    for k in keys_to_remove:
                        REPLY_MAPPING.pop(k, None)

            self.logger.info(f"Сообщение успешно переслано в Telegram (msg_id={sent_msg.message_id})")

        except Exception as e:
            self.logger.error(f"Ошибка пересылки в Telegram: {e}", exc_info=True)

        return False