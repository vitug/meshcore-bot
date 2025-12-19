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
+ ОТПРАВКА В TELEGRAM ПОЛНОСТЬЮ АСИНХРОННАЯ — НЕ БЛОКИРУЕТ ОСНОВНОЙ ЦИКЛ БОТА
  (убран future.result() — теперь обработка результата в фоновом потоке)
"""

import asyncio
import threading
import re
from typing import Dict, Optional
from .base_command import BaseCommand
from ..models import MeshMessage
from threading import Lock

# Глобальный словарь: telegram_message_id → исходное MeshMessage (для ответов)
REPLY_MAPPING: Dict[tuple, MeshMessage] = {}

# REPLY_MAPPING_LOCK = asyncio.Lock()
# REPLY_MAPPING_LOCK = Lock()
# ДВА РАЗНЫХ ЛОКА:
REPLY_MAPPING_LOCK_SYNC = Lock()           # Для синхронного кода (_handle_send_result)
REPLY_MAPPING_LOCK_ASYNC = asyncio.Lock()  # Для асинхронного кода (handle_telegram_message)

REPLY_MAPPING_MAX_MESSAGES = 1000

# Множество чатов, куда уже отправляли chat_id
CHAT_ID_SENT: set = set()

# Настраиваемые разделители для имени в DM (можно менять на любые)
DM_NAME_DELIMITERS = ('"', '"')  # открывающий и закрывающий символ
# Альтернативы:
# DM_NAME_DELIMITERS = ('[', ']')  # квадратные скобки
# DM_NAME_DELIMITERS = ('«', '»')  # ёлочки
# DM_NAME_DELIMITERS = ("'", "'")  # одинарные кавычки

def get_async_lock():
    """Ленивая инициализация asyncio.Lock в правильном event loop"""
    global REPLY_MAPPING_LOCK_ASYNC
    if REPLY_MAPPING_LOCK_ASYNC is None:
        REPLY_MAPPING_LOCK_ASYNC = asyncio.Lock()
    return REPLY_MAPPING_LOCK_ASYNC
    
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
        self.default_channel = bot.config.get('Telegram_Bridge', 'default_channel', fallback=None)  # добавлено: канал по умолчанию для отправки из TG
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

        # Persist reply mapping в db (адаптация для MeshCore: persistent, чтобы пережить рестарт)
        self.persist_reply_mapping = self.bot.config.getboolean('Telegram_Bridge', 'persist_reply_mapping', fallback=True)
        self.mapping_ttl_days = self.bot.config.getint('Telegram_Bridge', 'mapping_ttl_days', fallback=7)

        if self.persist_reply_mapping:
            try:
                # Создаём таблицу (синхронно — db_manager синхронный)
                self.bot.db_manager.create_table('reply_mapping', '''
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    msg_id INTEGER NOT NULL,
                    sender_id TEXT NOT NULL,
                    channel TEXT,
                    is_dm BOOLEAN NOT NULL,
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ''')

                self.bot.db_manager.execute_update('''
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_key ON reply_mapping (chat_id, msg_id)
                ''')

                # Чистка старых записей
                ttl_seconds = self.mapping_ttl_days * 86400
                self.bot.db_manager.execute_update('''
                    DELETE FROM reply_mapping 
                    WHERE timestamp < datetime('now', '-' || ? || ' seconds')
                ''', (ttl_seconds,))

                self.logger.info(f"Persist reply mapping включён (TTL: {self.mapping_ttl_days} дней). Таблица подготовлена.")

            except Exception as e:
                self.logger.error(f"Ошибка инициализации persist mapping: {e}", exc_info=True)          
    
    async def load_reply_mapping_from_db(self):
        """Асинхронная загрузка mapping из БД в память при старте"""
        if not self.persist_reply_mapping:
            return

        try:
            rows = self.bot.db_manager.execute_query('''
                SELECT chat_id, msg_id, sender_id, channel, is_dm, content
                FROM reply_mapping
                ORDER BY timestamp DESC
            ''')

            async with get_async_lock():
                loaded_count = 0
                for row in rows:
                    key = (row['chat_id'], row['msg_id'])
                    REPLY_MAPPING[key] = MeshMessage(
                        content=row['content'] or '',
                        sender_id=row['sender_id'],
                        channel=row['channel'],
                        is_dm=bool(row['is_dm'])
                    )
                    loaded_count += 1

            self.logger.info(f"Успешно загружено {loaded_count} записей reply mapping из БД в память")
        except Exception as e:
            self.logger.error(f"Ошибка при загрузке reply mapping из БД: {e}", exc_info=True)
        
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
        """Парсит настройку forward_channels из config.ini"""
        raw = self.bot.config.get('Telegram_Bridge', 'forward_channels', fallback='all').strip().lower()
        self.logger.debug(f"Raw forward_channels из config: '{raw}'")
        if raw in ['all', '*', '']:
            self.logger.info("forward_channels = all → пересылаем ВСЕ каналы")
            return set()  # пустой set означает "все каналы"
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
            self.tg_bot = AsyncTeleBot(self.telegram_token)
            
            # Получаем разделители
            open_delim, close_delim = DM_NAME_DELIMITERS

            # Команда /status — работает в любом регистре
            @self.tg_bot.message_handler(commands=['status', 'STATUS'])
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

            # Команда /start — автоотправка chat_id (работает в любом регистре)
            @self.tg_bot.message_handler(commands=['start', 'START'])
            async def handle_start(tg_message):
                if tg_message.chat.id in CHAT_ID_SENT:
                    return  # уже отправляли — не спамим
                welcome_text = (
                    f"Привет! Это мост MeshCore ↔ Telegram.\n"
                    f"<b>Chat ID этого чата:</b> <code>{tg_message.chat.id}</code>\n\n"
                    f"Скопируйте и вставьте в config.ini:\n"
                    f"<code>telegram_chat_id = {tg_message.chat.id}</code>\n\n"
                    f"После перезапуска — сообщения из сети придут сюда.\n"
                    f"Команды:\n"
                    f"/ch #general текст — в канал\n"
                    f"/dm m4Sokol текст — в личку по ID\n"
                    f"/dm {open_delim}Shiva Sodedi{close_delim} текст — в личку по имени\n"
                    f"/status — проверить состояние"
                )
                await self.tg_bot.reply_to(tg_message, welcome_text, parse_mode='HTML')
                CHAT_ID_SENT.add(tg_message.chat.id)
                self.logger.info(f"Автоматически отправлен chat_id {tg_message.chat.id}")

            # Команды /ch и /dm — работают в любом регистре
            @self.tg_bot.message_handler(commands=['ch', 'CH', 'dm', 'DM'])
            async def handle_send_commands(tg_message):
                if not (self.telegram_chat_id and str(tg_message.chat.id) == self.telegram_chat_id):
                    return  # только в целевом чате

                full_text = tg_message.text or ""
                
                # Извлекаем команду
                first_space = full_text.find(' ')
                if first_space == -1:
                    await self.tg_bot.reply_to(
                        tg_message, 
                        f"Использование:\n"
                        f"/ch #канал сообщение\n"
                        f"/dm node_id сообщение\n"
                        f"/dm {open_delim}Имя Фамилия{close_delim} сообщение"
                    )
                    return
                
                raw_command = full_text[1:first_space].lower()  # 'ch' или 'dm'
                rest = full_text[first_space + 1:].strip()      # всё после команды
                
                if not rest:
                    await self.tg_bot.reply_to(tg_message, f"Использование: /{raw_command} <цель> <сообщение>")
                    return
                
                target_arg = ""
                message_text = ""
                
                # Парсинг аргументов с учётом настраиваемых разделителей
                if rest.startswith(open_delim):
                    # Ищем закрывающий разделитель
                    delim_end = rest.find(close_delim, 1)  # ищем после первого символа
                    if delim_end == -1:
                        await self.tg_bot.reply_to(
                            tg_message, 
                            f"❌ Не найден закрывающий разделитель `{close_delim}`"
                        )
                        return
                    target_arg = rest[:delim_end + 1]           # "Имя Фамилия" с разделителями
                    message_text = rest[delim_end + 1:].strip() # текст после разделителя
                else:
                    # Обычный формат: цель сообщение
                    parts = rest.split(maxsplit=1)
                    target_arg = parts[0]
                    message_text = parts[1] if len(parts) > 1 else ""
                
                # Проверка наличия текста сообщения
                if not message_text:
                    await self.tg_bot.reply_to(tg_message, f"❌ Не указан текст сообщения")
                    return
                
                # Формируем подпись отправителя
                sender_name = (
                    tg_message.from_user.full_name or 
                    tg_message.from_user.username or 
                    "TG-User"
                )
                formatted_message = f"TG: {message_text}"
                self.logger.info(f"Отправляем сообщение из тг от: {sender_name}")
                
                # === Обработка /ch ===
                if raw_command == 'ch':
                    channel_name = target_arg.lstrip('#')
                    try:
                        await self.bot.command_manager.send_channel_message(channel_name, formatted_message)
                        await self.tg_bot.reply_to(tg_message, f"✅ Отправлено в канал #{channel_name}")
                        self.logger.info(f"TG → канал #{channel_name}: {message_text}")
                    except Exception as e:
                        await self.tg_bot.reply_to(tg_message, f"❌ Ошибка отправки: {e}")
                        self.logger.error(f"Ошибка отправки в канал: {e}")
                
                # === Обработка /dm ===
                elif raw_command == 'dm':
                    target_node_id = None
                    display_target = target_arg
                    
                    # Проверяем, заключено ли имя в разделители
                    if target_arg.startswith(open_delim) and target_arg.endswith(close_delim):
                        # Поиск по имени — убираем разделители
                        target_name = target_arg[1:-1].strip()
                        target_node_id = target_name
                        display_target = f"{target_arg} ({target_node_id})"
                    else:
                        # Прямой node_id
                        target_node_id = target_arg
                    
                    try:
                        success = await self.bot.command_manager.send_dm(target_node_id, formatted_message)
                        if success:
                            await self.tg_bot.reply_to(tg_message, f"✅ Отправлено в DM → {display_target}")
                            self.logger.info(f"TG → DM {display_target}: {message_text}")
                        else:
                            await self.tg_bot.reply_to(tg_message, f"❌ сообщение не отправлено: {display_target}")
                            self.logger.error(f"сообщение не отправлено для {display_target} ({target_node_id})")                        
                    except Exception as e:
                        await self.tg_bot.reply_to(tg_message, f"❌ Ошибка отправки: {e}")
                        self.logger.error(f"Ошибка отправки DM: {e}")

            # Обработка всех остальных сообщений
            @self.tg_bot.message_handler(func=lambda m: True)
            async def handle_telegram_message(tg_message):
                chat_id_str = str(tg_message.chat.id)

                # Ответы из TG в MeshCore (reply на сообщение бота)
                if (tg_message.reply_to_message and
                    tg_message.reply_to_message.from_user.is_bot and
                    self.telegram_chat_id and
                    chat_id_str == self.telegram_chat_id):
                    async with get_async_lock():
                        key = (tg_message.chat.id, tg_message.reply_to_message.message_id)
                        original_mesh_msg = REPLY_MAPPING.get(key)
                        
                    if self.persist_reply_mapping and original_mesh_msg is None:
                        # Fallback на db
                        db_result = self.bot.db_manager.execute_query('''
                            SELECT sender_id, channel, is_dm, content 
                            FROM reply_mapping 
                            WHERE chat_id = ? AND msg_id = ?
                        ''', (key[0], key[1]))
                        if db_result:
                            row = db_result[0]
                            original_mesh_msg = MeshMessage(  # Реконструируем объект из models.py
                                content=row['content'] or '',
                                sender_id=row['sender_id'],
                                channel=row['channel'],
                                is_dm=bool(row['is_dm'])
                            )
                            self.logger.info(f"Mapping восстановлен из db для key={key}")
        
                    if original_mesh_msg is None:
                        # логируем содержимое mapping при ошибке
                        self.logger.warning(f"Mapping не найден для reply (key={key})")
                        async with get_async_lock():
                            if REPLY_MAPPING:
                                self.logger.debug("Текущее содержимое REPLY_MAPPING (последние 10 записей):")
                                # Выводим только последние 10 элементов, чтобы не засорять лог
                                for map_key, map_value in list(REPLY_MAPPING.items())[-10:]:
                                    self.logger.debug(
                                        f"  Ключ: {map_key} | "
                                        f"sender_id={getattr(map_value, 'sender_id', 'None')} | "
                                        f"channel={getattr(map_value, 'channel', 'None')} | "
                                        f"is_dm={getattr(map_value, 'is_dm', 'None')}"
                                    )
                            else:
                                self.logger.debug("REPLY_MAPPING полностью пустой")
                        # Уведомляем пользователя в Telegram
                        await self.tg_bot.reply_to(tg_message, "❌ Ответ не доставлен: исходное сообщение устарело или mapping потерян")
                        return  # Важно: прерываем обработку, чтобы не уйти в default_channel
                     
                    if original_mesh_msg:
                        sender_id = original_mesh_msg.sender_id
                        is_dm = original_mesh_msg.is_dm
                        response_text = tg_message.text or tg_message.caption or "[медиа/стикер]"
                        if is_dm:
                            reply_text = f"TG:{response_text}"
                        else:
                            reply_text = f"TG:@[{sender_id}] {response_text}"
                        if original_mesh_msg.is_dm:
                            await self.bot.command_manager.send_dm(original_mesh_msg.sender_id, reply_text)
                        else:
                            await self.bot.command_manager.send_channel_message(original_mesh_msg.channel, reply_text)
                        self.logger.info(f"Ответ из TG отправлен в MeshCore")
                        return

                # Обычные сообщения (не команда и не ответ) → в default_channel
                if self.telegram_chat_id and chat_id_str == self.telegram_chat_id and self.default_channel:
                    text = tg_message.text or tg_message.caption or "[медиа/стикер]"
                    if text.strip():
                        full_text = f"TG: {text}"
                        await self.bot.command_manager.send_channel_message(self.default_channel, full_text)
                        self.logger.info(f"Сообщение из TG отправлено в default_channel #{self.default_channel}")

            # Запуск поллинга в отдельном потоке
            def run_polling():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.tg_loop = loop
                loop.run_until_complete(self.tg_bot.polling(none_stop=True, interval=1, timeout=60))
            threading.Thread(target=run_polling, daemon=True).start()
            self.logger.info("Telegram поллинг запущен в отдельном потоке")
        except Exception as e:
            self.logger.error(f"Ошибка инициализации Telegram бота: {e}")
            self.enabled = False

    async def _save_reply_mapping(self, sent_msg, original_mesh_msg: MeshMessage):
        """
        Потокобезопасное сохранение маппинга telegram_message_id → MeshMessage
        Вызывается из фонового потока, но сохраняет данные в основном loop бота
        """    
        self.logger.info(f"Начало сохранения mapping для msg_id={sent_msg.message_id} от {original_mesh_msg.sender_id}")

        async with get_async_lock():
            key = (sent_msg.chat.id, sent_msg.message_id)
            REPLY_MAPPING[key] = original_mesh_msg
            self.logger.info(f"Mapping сохранён в памяти для key={key} (всего записей: {len(REPLY_MAPPING)})")

            if len(REPLY_MAPPING) > REPLY_MAPPING_MAX_MESSAGES:
                keys_to_remove = sorted(REPLY_MAPPING.keys(), key=lambda k: k[1])[:200]
                for k in keys_to_remove:
                    REPLY_MAPPING.pop(k, None)
                self.logger.info(f"Очищено 200 старых записей из памяти")

        if self.persist_reply_mapping:
            try:
                save_content = self.bot.config.getboolean('Telegram_Bridge', 'save_reply_content', fallback=False)
                content_value = original_mesh_msg.content if save_content else None

                self.bot.db_manager.execute_update('''
                    INSERT OR REPLACE INTO reply_mapping
                    (chat_id, msg_id, sender_id, channel, is_dm, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    key[0], key[1],
                    original_mesh_msg.sender_id or '',
                    original_mesh_msg.channel,
                    int(original_mesh_msg.is_dm),
                    content_value
                ))
                self.logger.info(f"Mapping успешно сохранён в БД для key={key}")
            except Exception as db_e:
                self.logger.error(f"Ошибка сохранения mapping в БД: {db_e}", exc_info=True)                  

    async def _send_to_telegram_non_blocking(self, chat_id: str, text: str, original_message: MeshMessage):
        """
        Отправка сообщения в Telegram БЕЗ БЛОКИРОВКИ основного цикла бота.
        Основной поток только ставит задачу в очередь и сразу возвращается.
        Обработка результата (включая сохранение REPLY_MAPPING) происходит в фоновом потоке.
        """
        if not self.tg_bot or not self.tg_loop:
            self.logger.error("Telegram бот или loop не инициализированы")
            return

        # Формируем корутину отправки
        coro = self.tg_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode='HTML',
            disable_web_page_preview=True
        )

        # Запускаем её в Telegram-loop (другой поток)
        future = asyncio.run_coroutine_threadsafe(coro, self.tg_loop)

        # Замыкаем original_mesh_msg в локальную переменную, чтобы она была доступна в _handle_send_result
        original_mesh_msg = original_message
        
        # Фоновая функция для обработки результата отправки
        def _handle_send_result():
            try:
                sent_msg = future.result(timeout=30)
                if sent_msg:
                    self.logger.debug(f"Успешная отправка в TG, msg_id={sent_msg.message_id} — сохраняем mapping синхронно")

                    key = (sent_msg.chat.id, sent_msg.message_id)
                    if not original_mesh_msg==None:
                        REPLY_MAPPING_LOCK_SYNC.acquire()
                        try:
                            REPLY_MAPPING[key] = original_mesh_msg
                            self.logger.debug(f"Mapping сохранён в памяти для key={key} (всего: {len(REPLY_MAPPING)})")

                            if len(REPLY_MAPPING) > REPLY_MAPPING_MAX_MESSAGES:
                                keys_to_remove = sorted(REPLY_MAPPING.keys(), key=lambda k: k[1])[:200]
                                for k in keys_to_remove:
                                    REPLY_MAPPING.pop(k, None)
                                self.logger.debug("Очищено 200 старых записей из памяти")
                        finally:
                            REPLY_MAPPING_LOCK_SYNC.release()

                        if self.persist_reply_mapping:
                            try:
                                save_content = self.bot.config.getboolean('Telegram_Bridge', 'save_reply_content', fallback=False)
                                content_value = original_mesh_msg.content if save_content else None

                                self.bot.db_manager.execute_update('''
                                    INSERT OR REPLACE INTO reply_mapping
                                    (chat_id, msg_id, sender_id, channel, is_dm, content)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                ''', (
                                    key[0], key[1],
                                    original_mesh_msg.sender_id or '',
                                    original_mesh_msg.channel,
                                    int(original_mesh_msg.is_dm),
                                    content_value
                                ))
                                self.logger.info(f"Mapping успешно сохранён в БД для key={key}")
                            except Exception as db_e:
                                self.logger.error(f"Ошибка сохранения в БД: {db_e}", exc_info=True)

                    self.logger.info(f"Сообщение успешно переслано в Telegram (msg_id={sent_msg.message_id})")
                else:
                    self.logger.warning("Telegram API вернул None — mapping не сохранён")
            except Exception as e:
                self.logger.error(f"Ошибка в _handle_send_result: {e}", exc_info=True)

        # Запускаем обработку результата в отдельном daemon-потоке
        threading.Thread(target=_handle_send_result, daemon=True).start()

    async def execute(self, message: MeshMessage) -> bool:
        """
        Пересылка сообщений из MeshCore → Telegram
        Теперь полностью асинхронная — основной цикл бота не ждёт ответа от Telegram API
        """
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

            # АСИНХРОННАЯ ОТПРАВКА: основной поток НЕ ЖДЁТ результата
            asyncio.create_task(
                self._send_to_telegram_non_blocking(
                    chat_id=self.telegram_chat_id,
                    text=full_text,
                    original_message=message
                )
            )
            self.logger.debug("Сообщение поставлено в очередь на отправку в Telegram (неблокирующе)")

        except Exception as e:
            self.logger.error(f"Ошибка подготовки пересылки в Telegram: {e}", exc_info=True)

        return False