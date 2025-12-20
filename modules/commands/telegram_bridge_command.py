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
+ АВТОМАТИЧЕСКАЯ РАЗБИВКА СООБЩЕНИЙ ИЗ TG НА ЧАСТИ ПО 140 БАЙТ (макс. 3 сообщения)
+ ТРАНСЛИТЕРАЦИЯ кириллицы:
  - Автоматическая (auto_translit=true) — если сообщение не помещается в лимит
  - Принудительная (/tl команда) — всегда транслитерирует
"""

import asyncio
import threading
import re
from typing import Dict, Optional, List, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage
from threading import Lock

# Глобальный словарь: telegram_message_id → исходное MeshMessage (для ответов)
REPLY_MAPPING: Dict[tuple, MeshMessage] = {}

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
# DM_NAME_DELIMITERS = ("'", "'")  # одинарные кавычки

# Константы для разбивки сообщений, могут перекрываться конфигом.
MAX_MESH_MESSAGE_BYTES = 140  # Максимальный размер одного сообщения в байтах
MAX_MESH_MESSAGES = 3         # Максимальное количество сообщений

# ============================================================================
# ТАБЛИЦА ТРАНСЛИТЕРАЦИИ КИРИЛЛИЦЫ В ЛАТИНИЦУ
# ============================================================================
TRANSLIT_TABLE = {
    'А': 'A', 'а': 'a', 'Б': 'B', 'б': 'b', 'В': 'V', 'в': 'v',
    'Г': 'G', 'г': 'g', 'Д': 'D', 'д': 'd', 'Е': 'E', 'е': 'e',
    'Ё': 'Yo', 'ё': 'yo', 'Ж': 'Zh', 'ж': 'zh', 'З': 'Z', 'з': 'z',
    'И': 'I', 'и': 'i', 'Й': 'Y', 'й': 'y', 'К': 'K', 'к': 'k',
    'Л': 'L', 'л': 'l', 'М': 'M', 'м': 'm', 'Н': 'N', 'н': 'n',
    'О': 'O', 'о': 'o', 'П': 'P', 'п': 'p', 'Р': 'R', 'р': 'r',
    'С': 'S', 'с': 's', 'Т': 'T', 'т': 't', 'У': 'U', 'у': 'u',
    'Ф': 'F', 'ф': 'f', 'Х': 'Kh', 'х': 'kh', 'Ц': 'Ts', 'ц': 'ts',
    'Ч': 'Ch', 'ч': 'ch', 'Ш': 'Sh', 'ш': 'sh', 'Щ': 'Sch', 'щ': 'sch',
    'Ъ': '', 'ъ': '', 'Ы': 'Y', 'ы': 'y', 'Ь': '', 'ь': '',
    'Э': 'E', 'э': 'e', 'Ю': 'Yu', 'ю': 'yu', 'Я': 'Ya', 'я': 'ya',
}


def transliterate_ru_to_en(text: str) -> str:
    """
    Транслитерирует русские буквы в латиницу.
    Остальные символы (латиница, цифры, знаки) остаются без изменений.
    
    Пример: "Привет мир!" -> "Privet mir!"
    """
    result = []
    for char in text:
        result.append(TRANSLIT_TABLE.get(char, char))
    return ''.join(result)


def has_cyrillic(text: str) -> bool:
    """Проверяет, содержит ли текст кириллические символы."""
    return any(char in TRANSLIT_TABLE for char in text)


def get_async_lock():
    """Ленивая инициализация asyncio.Lock в правильном event loop"""
    global REPLY_MAPPING_LOCK_ASYNC
    if REPLY_MAPPING_LOCK_ASYNC is None:
        REPLY_MAPPING_LOCK_ASYNC = asyncio.Lock()
    return REPLY_MAPPING_LOCK_ASYNC


def split_message_by_bytes(text: str, max_bytes: int = MAX_MESH_MESSAGE_BYTES, 
                            max_parts: int = MAX_MESH_MESSAGES) -> List[str]:
    """
    Разбивает текст на части, каждая из которых не превышает max_bytes байт в UTF-8.
    Возвращает не более max_parts частей.
    """
    if not text:
        return []
    
    if len(text.encode('utf-8')) <= max_bytes:
        return [text]
    
    parts = []
    current_part = ""
    current_bytes = 0
    
    for char in text:
        char_bytes = len(char.encode('utf-8'))
        
        if current_bytes + char_bytes > max_bytes:
            if current_part:
                parts.append(current_part)
                if len(parts) >= max_parts:
                    break
            current_part = char
            current_bytes = char_bytes
        else:
            current_part += char
            current_bytes += char_bytes
    
    if current_part and len(parts) < max_parts:
        parts.append(current_part)
    
    return parts


def split_message_by_bytes_smart(text: str, max_bytes: int = MAX_MESH_MESSAGE_BYTES,
                                  max_parts: int = MAX_MESH_MESSAGES) -> List[str]:
    """
    Умная разбивка текста — старается разбивать по пробелам/знакам препинания,
    а не посреди слова.
    """
    if not text:
        return []
    
    if len(text.encode('utf-8')) <= max_bytes:
        return [text]
    
    parts = []
    remaining = text
    
    while remaining and len(parts) < max_parts:
        if len(remaining.encode('utf-8')) <= max_bytes:
            parts.append(remaining)
            break
        
        split_point = _find_split_point(remaining, max_bytes)
        
        if split_point > 0:
            parts.append(remaining[:split_point].rstrip())
            remaining = remaining[split_point:].lstrip()
        else:
            forced_part = _force_split_by_bytes(remaining, max_bytes)
            parts.append(forced_part)
            remaining = remaining[len(forced_part):]
    
    return parts


def _find_split_point(text: str, max_bytes: int) -> int:
    """Находит лучшую точку разбивки в пределах max_bytes байт."""
    max_char_index = 0
    current_bytes = 0
    
    for i, char in enumerate(text):
        char_bytes = len(char.encode('utf-8'))
        if current_bytes + char_bytes > max_bytes:
            break
        current_bytes += char_bytes
        max_char_index = i + 1
    
    if max_char_index == 0:
        return 0
    
    search_text = text[:max_char_index]
    best_split = -1
    
    for i in range(len(search_text) - 1, -1, -1):
        char = search_text[i]
        if char in '.!?':
            return i + 1
        elif char in ',;:' and best_split == -1:
            best_split = i + 1
        elif char == ' ' and best_split == -1:
            best_split = i
    
    if best_split > max_char_index * 0.2:
        return best_split
    
    return max_char_index


def _force_split_by_bytes(text: str, max_bytes: int) -> str:
    """Принудительная разбивка по байтам."""
    result = ""
    current_bytes = 0
    
    for char in text:
        char_bytes = len(char.encode('utf-8'))
        if current_bytes + char_bytes > max_bytes:
            break
        result += char
        current_bytes += char_bytes
    
    return result

    
class TelegramBridgeCommand(BaseCommand):
    name = "telegram_bridge"
    keywords = []
    description = "Мост с Telegram: пересылает сообщения в TG и обратно + транслитерация"
    category = "system"
    requires_dm = False

    def __init__(self, bot):
        super().__init__(bot)
        self.enabled = bot.config.getboolean('Telegram_Bridge', 'enabled', fallback=False)
        self.telegram_chat_id = bot.config.get('Telegram_Bridge', 'telegram_chat_id', fallback=None)
        self.telegram_token = bot.config.get('Telegram_Bridge', 'telegram_token', fallback=None)
        self.default_channel = bot.config.get('Telegram_Bridge', 'default_channel', fallback=None)
        self.forward_channels = self._parse_forward_channels()
        self.include_dm = bot.config.getboolean('Telegram_Bridge', 'include_dm', fallback=False)
        self.prefix_sender = bot.config.getboolean('Telegram_Bridge', 'prefix_sender', fallback=True)
        
        # Настройки разбивки сообщений
        self.max_mesh_bytes = bot.config.getint('Telegram_Bridge', 'max_mesh_bytes', fallback=MAX_MESH_MESSAGE_BYTES)
        self.max_mesh_parts = bot.config.getint('Telegram_Bridge', 'max_mesh_parts', fallback=MAX_MESH_MESSAGES)
        self.smart_split = bot.config.getboolean('Telegram_Bridge', 'smart_split', fallback=True)
        
        # ====================================================================
        # НАСТРОЙКИ ТРАНСЛИТЕРАЦИИ
        # ====================================================================
        # auto_translit = true  — транслитерировать автоматически если не помещается
        # auto_translit = false — транслитерировать только по команде /tl
        # force_translit = true — ВСЕГДА транслитерировать все сообщения
        self.auto_translit = bot.config.getboolean('Telegram_Bridge', 'auto_translit', fallback=True)
        self.force_translit = bot.config.getboolean('Telegram_Bridge', 'force_translit', fallback=False)
        
        # Rate limit
        self.rate_limit_seconds = bot.config.getfloat('Bot', 'rate_limit_seconds', fallback=2.0)
        
        translit_mode = "принудительно" if self.force_translit else ("авто" if self.auto_translit else "по команде /tl")
        self.logger.info(
            f"Настройки: {self.max_mesh_bytes} байт, макс. {self.max_mesh_parts} частей, "
            f"транслит={translit_mode}, задержка {self.rate_limit_seconds}с"
        )
        
        if not self.enabled:
            self.logger.info("Telegram Bridge отключён в конфиге")
            return

        if not self.telegram_token:
            self.logger.warning("Telegram Bridge: не указан telegram_token — мост отключён")
            self.enabled = False
            return

        self.tg_bot = None
        self.tg_loop = None
        self._init_telegram_bot()

        if self.telegram_chat_id:
            self.logger.info(f"Telegram Bridge включён → целевой чат {self.telegram_chat_id}")
        else:
            self.logger.info("Telegram Bridge включён (chat_id не указан — только авто-определение)")
    
        # Persist reply mapping
        self.persist_reply_mapping = self.bot.config.getboolean('Telegram_Bridge', 'persist_reply_mapping', fallback=True)
        self.mapping_ttl_days = self.bot.config.getint('Telegram_Bridge', 'mapping_ttl_days', fallback=7)

        if self.persist_reply_mapping:
            try:
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

                ttl_seconds = self.mapping_ttl_days * 86400
                self.bot.db_manager.execute_update('''
                    DELETE FROM reply_mapping 
                    WHERE timestamp < datetime('now', '-' || ? || ' seconds')
                ''', (ttl_seconds,))

                self.logger.info(f"Persist reply mapping включён (TTL: {self.mapping_ttl_days} дней)")

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

            self.logger.info(f"Загружено {loaded_count} записей reply mapping из БД")
        except Exception as e:
            self.logger.error(f"Ошибка загрузки reply mapping: {e}", exc_info=True)
        
    def matches_keyword(self, message: MeshMessage) -> bool:
        return False

    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        return False

    def get_response_format(self) -> Optional[str]:
        return ""

    def _parse_forward_channels(self) -> set:
        """Парсит настройку forward_channels из config.ini"""
        raw = self.bot.config.get('Telegram_Bridge', 'forward_channels', fallback='all').strip().lower()
        if raw in ['all', '*', '']:
            self.logger.info("forward_channels = all → пересылаем ВСЕ каналы")
            return set()
        channels = {ch.strip() for ch in raw.split(',') if ch.strip()}
        self.logger.info(f"forward_channels: {channels}")
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

    def _split_outgoing_message(self, text: str) -> List[str]:
        """Разбивает исходящее сообщение на части."""
        if self.smart_split:
            return split_message_by_bytes_smart(text, self.max_mesh_bytes, self.max_mesh_parts)
        else:
            return split_message_by_bytes(text, self.max_mesh_bytes, self.max_mesh_parts)

    # ========================================================================
    # УНИВЕРСАЛЬНЫЙ МЕТОД ПОДГОТОВКИ И ОТПРАВКИ СООБЩЕНИЙ
    # ========================================================================
    async def _prepare_and_send_to_mesh(
        self,
        message_text: str,
        target: str,
        is_dm: bool,
        prefix: str = "TG: ",
        force_translit: bool = False
    ) -> Tuple[int, int, bool]:
        """
        Подготавливает и отправляет сообщение в MeshCore.
        
        Логика транслитерации:
        1. force_translit=True (команда /tl) — ВСЕГДА транслитерировать
        2. self.force_translit=True (конфиг) — ВСЕГДА транслитерировать все
        3. self.auto_translit=True — транслитерировать если не помещается в лимит
        4. Иначе — не транслитерировать
        
        Args:
            message_text: Текст сообщения (без префикса)
            target: Цель — канал (с # или без) или node_id для DM
            is_dm: True для личных сообщений, False для канала
            prefix: Префикс сообщения (например "TG: " или "TG:@[id] ")
            force_translit: Принудительная транслитерация (команда /tl)
        
        Returns:
            Tuple (sent_count, total_parts, was_transliterated):
            - sent_count: сколько частей успешно отправлено
            - total_parts: общее количество частей
            - was_transliterated: была ли применена транслитерация
        """
        was_transliterated = False
        text_to_send = message_text
        
        # Определяем нужна ли транслитерация
        should_translit = False
        
        if force_translit or self.force_translit:
            # Принудительная транслитерация (команда /tl или force_translit в конфиге)
            should_translit = has_cyrillic(text_to_send)
            if should_translit:
                self.logger.debug("Принудительная транслитерация активирована")
        elif self.auto_translit:
            # Автоматическая транслитерация — только если не помещается
            full_test = f"{prefix}{text_to_send}"
            full_size = len(full_test.encode('utf-8'))
            
            if full_size > self.max_mesh_bytes and has_cyrillic(text_to_send):
                should_translit = True
                self.logger.debug(f"Авто-транслитерация: {full_size} байт > {self.max_mesh_bytes}")
        
        # Применяем транслитерацию если нужно
        if should_translit:
            original_size = len(f"{prefix}{text_to_send}".encode('utf-8'))
            text_to_send = transliterate_ru_to_en(text_to_send)
            was_transliterated = True
            new_size = len(f"{prefix}{text_to_send}".encode('utf-8'))
            self.logger.info(
                f"Транслитерация применена: {original_size} байт → {new_size} байт"
            )
        
        # Разбиваем на части
        message_parts = self._split_outgoing_message(text_to_send)
        total_parts = len(message_parts)
        
        sent_count = 0
        for i, part in enumerate(message_parts):
            # Формируем сообщение с номером части если нужно
            if total_parts > 1:
                formatted_message = f"{prefix}[{i+1}/{total_parts}] {part}"
            else:
                formatted_message = f"{prefix}{part}"
            
            try:
                if is_dm:
                    success = await self.bot.command_manager.send_dm(target, formatted_message)
                    if not success:
                        self.logger.error(f"Не удалось отправить DM часть {i+1}/{total_parts} для {target}")
                        break
                else:
                    await self.bot.command_manager.send_channel_message(target, formatted_message)
                
                sent_count += 1
                target_type = "DM" if is_dm else f"канал"
                self.logger.info(
                    f"TG → {target_type} {target} "
                    f"({'транслит ' if was_transliterated else ''}"
                    f"часть {i+1}/{total_parts}): {part[:50]}..."
                )
                
                # Пауза между частями
                if i < total_parts - 1:
                    await asyncio.sleep(self.rate_limit_seconds)
                    
            except Exception as e:
                self.logger.error(f"Ошибка отправки части {i+1}/{total_parts}: {e}")
                break
        
        return sent_count, total_parts, was_transliterated

    def _format_send_result(
        self, 
        sent_count: int, 
        total_parts: int, 
        was_transliterated: bool,
        original_bytes: int,
        target_display: str,
        is_dm: bool
    ) -> str:
        """Форматирует сообщение о результате отправки для ответа в Telegram."""
        target_type = "DM" if is_dm else "канал"
        
        if sent_count == 0:
            return f"❌ Не удалось отправить сообщение в {target_type} {target_display}"
        
        if sent_count < total_parts:
            return f"⚠️ Отправлено {sent_count}/{total_parts} частей в {target_type} → {target_display}"
        
        # Полный успех
        result = f"✅ Отправлено в {target_type} {'→ ' if is_dm else ''}{target_display}"
        
        details = []
        if was_transliterated:
            details.append("🔤 транслит")
        if total_parts > 1:
            details.append(f"📦 {total_parts} частей")
        if original_bytes > self.max_mesh_bytes:
            details.append(f"{original_bytes} байт")
        
        if details:
            result += f"\n{', '.join(details)}"
        
        return result

    def _init_telegram_bot(self):
        """Инициализация AsyncTeleBot с запуском поллинга в отдельном потоке"""
        try:
            from telebot.async_telebot import AsyncTeleBot
            self.tg_bot = AsyncTeleBot(self.telegram_token)
            
            open_delim, close_delim = DM_NAME_DELIMITERS

            @self.tg_bot.message_handler(commands=['status', 'STATUS'])
            async def handle_status(tg_message):
                conn_status = "🟢 Подключено" if hasattr(self.bot.meshcore, 'connected') and self.bot.meshcore.connected else "🔴 Отключено"
                bridge_status = "✅ Активен" if self.enabled and self.telegram_chat_id else "⚠️ Ожидает chat_id"
                target_chat = self.telegram_chat_id or "не указан"
                
                # Статус транслитерации
                if self.force_translit:
                    translit_status = "🔤 Принудительно (все сообщения)"
                elif self.auto_translit:
                    translit_status = "🔄 Авто (если не помещается)"
                else:
                    translit_status = "📝 По команде /tl"
                
                status_text = (
                    f"📡 <b>Статус Telegram Bridge</b>\n\n"
                    f"Мост: {bridge_status}\n"
                    f"MeshCore: {conn_status}\n"
                    f"Целевой чат: <code>{target_chat}</code>\n"
                    f"Текущий чат: <code>{tg_message.chat.id}</code>\n"
                    f"Макс. байт: {self.max_mesh_bytes}, макс. частей: {self.max_mesh_parts}\n"
                    f"Транслитерация: {translit_status}\n\n"
                    f"<b>Команды:</b>\n"
                    f"/ch #канал текст — в канал\n"
                    f"/dm node_id текст — в личку\n"
                    f"/tlch #канал текст — в канал с транслитом\n"
                    f"/tldm node_id текст — в личку с транслитом"
                )
                await self.tg_bot.reply_to(tg_message, status_text, parse_mode='HTML')

            @self.tg_bot.message_handler(commands=['start', 'START'])
            async def handle_start(tg_message):
                if tg_message.chat.id in CHAT_ID_SENT:
                    return
                    
                # Статус транслитерации для приветствия
                if self.force_translit:
                    translit_note = "\n🔤 Все сообщения автоматически транслитерируются (force_translit=true)."
                elif self.auto_translit:
                    translit_note = "\n🔄 Длинные сообщения на кириллице автоматически транслитерируются."
                else:
                    translit_note = "\n📝 Для транслитерации используйте команды /tlch и /tldm."
                
                welcome_text = (
                    f"Привет! Это мост MeshCore ↔ Telegram.\n"
                    f"<b>Chat ID этого чата:</b> <code>{tg_message.chat.id}</code>\n\n"
                    f"Скопируйте и вставьте в config.ini:\n"
                    f"<code>telegram_chat_id = {tg_message.chat.id}</code>\n\n"
                    f"После перезапуска — сообщения из сети придут сюда.\n\n"
                    f"<b>Команды:</b>\n"
                    f"/ch #general текст — в канал\n"
                    f"/dm m4Sokol текст — в личку по ID\n"
                    f"/dm {open_delim}Имя Фамилия{close_delim} текст — в личку по имени\n"
                    f"/tlch #general текст — в канал <b>с транслитом</b>\n"
                    f"/tldm m4Sokol текст — в личку <b>с транслитом</b>\n"
                    f"/status — проверить состояние\n\n"
                    f"⚠️ Длинные сообщения разбиваются на части по {self.max_mesh_bytes} байт (макс. {self.max_mesh_parts} шт.)"
                    f"{translit_note}"
                )
                await self.tg_bot.reply_to(tg_message, welcome_text, parse_mode='HTML')
                CHAT_ID_SENT.add(tg_message.chat.id)

            # ================================================================
            # ОБРАБОТЧИК КОМАНД: /ch, /dm, /tlch, /tldm
            # ================================================================
            @self.tg_bot.message_handler(commands=['ch', 'CH', 'dm', 'DM', 'tlch', 'Tlch', 'tldm', 'Tldm'])
            async def handle_send_commands(tg_message):
                if not (self.telegram_chat_id and str(tg_message.chat.id) == self.telegram_chat_id):
                    return

                full_text = tg_message.text or ""
                
                first_space = full_text.find(' ')
                if first_space == -1:
                    await self.tg_bot.reply_to(
                        tg_message, 
                        f"<b>Использование:</b>\n"
                        f"/ch #канал сообщение — в канал\n"
                        f"/dm node_id сообщение — в личку\n"
                        f"/tlch #канал сообщение — в канал с транслитом\n"
                        f"/tldm node_id сообщение — в личку с транслитом\n"
                        f"/dm {open_delim}Имя Фамилия{close_delim} сообщение — в личку по имени",
                        parse_mode='HTML'
                    )
                    return
                
                raw_command = full_text[1:first_space].lower()
                rest = full_text[first_space + 1:].strip()
                
                if not rest:
                    await self.tg_bot.reply_to(tg_message, f"Использование: /{raw_command} <цель> <сообщение>")
                    return
                
                # Определяем тип команды
                force_translit_cmd = raw_command in ['tlch', 'tldm']
                is_dm_cmd = raw_command in ['dm', 'tldm']
                
                # Парсинг target и message_text
                if rest.startswith(open_delim):
                    delim_end = rest.find(close_delim, 1)
                    if delim_end == -1:
                        await self.tg_bot.reply_to(tg_message, f"❌ Не найден закрывающий разделитель `{close_delim}`")
                        return
                    target_arg = rest[:delim_end + 1]
                    message_text = rest[delim_end + 1:].strip()
                else:
                    parts = rest.split(maxsplit=1)
                    target_arg = parts[0]
                    message_text = parts[1] if len(parts) > 1 else ""
                
                if not message_text:
                    await self.tg_bot.reply_to(tg_message, f"❌ Не указан текст сообщения")
                    return
                
                original_bytes = len(message_text.encode('utf-8'))
                prefix = "TG: "
                
                # === /ch или /tlch — отправка в канал ===
                if not is_dm_cmd:
                    channel_name = target_arg
                    
                    try:
                        sent_count, total_parts, was_translit = await self._prepare_and_send_to_mesh(
                            message_text=message_text,
                            target=channel_name,
                            is_dm=False,
                            prefix=prefix,
                            force_translit=force_translit_cmd
                        )
                        
                        result_msg = self._format_send_result(
                            sent_count, total_parts, was_translit, 
                            original_bytes, f"#{channel_name}", is_dm=False
                        )
                        await self.tg_bot.reply_to(tg_message, result_msg)
                        
                    except Exception as e:
                        await self.tg_bot.reply_to(tg_message, f"❌ Ошибка отправки: {e}")
                        self.logger.error(f"Ошибка отправки в канал: {e}")
                
                # === /dm или /tldm — отправка в личку ===
                else:
                    display_target = target_arg
                    
                    if target_arg.startswith(open_delim) and target_arg.endswith(close_delim):
                        target_node_id = target_arg[1:-1].strip()
                        display_target = f"{target_arg}"
                    else:
                        target_node_id = target_arg
                    
                    try:
                        sent_count, total_parts, was_translit = await self._prepare_and_send_to_mesh(
                            message_text=message_text,
                            target=target_node_id,
                            is_dm=True,
                            prefix=prefix,
                            force_translit=force_translit_cmd
                        )
                        
                        result_msg = self._format_send_result(
                            sent_count, total_parts, was_translit,
                            original_bytes, display_target, is_dm=True
                        )
                        await self.tg_bot.reply_to(tg_message, result_msg)
                        
                    except Exception as e:
                        await self.tg_bot.reply_to(tg_message, f"❌ Ошибка отправки: {e}")
                        self.logger.error(f"Ошибка отправки DM: {e}")

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
                        
                    # Fallback на БД
                    if self.persist_reply_mapping and original_mesh_msg is None:
                        db_result = self.bot.db_manager.execute_query('''
                            SELECT sender_id, channel, is_dm, content 
                            FROM reply_mapping 
                            WHERE chat_id = ? AND msg_id = ?
                        ''', (key[0], key[1]))
                        if db_result:
                            row = db_result[0]
                            original_mesh_msg = MeshMessage(
                                content=row['content'] or '',
                                sender_id=row['sender_id'],
                                channel=row['channel'],
                                is_dm=bool(row['is_dm'])
                            )
        
                    if original_mesh_msg is None:
                        self.logger.warning(f"Mapping не найден для reply (key={key})")
                        await self.tg_bot.reply_to(tg_message, "❌ Ответ не доставлен: исходное сообщение устарело")
                        return
                     
                    # Формируем ответ
                    sender_id = original_mesh_msg.sender_id
                    is_dm = original_mesh_msg.is_dm
                    response_text = tg_message.text or tg_message.caption or "[медиа/стикер]"
                    
                    if is_dm:
                        prefix = "TG:"
                        target = sender_id
                    else:
                        prefix = f"TG:@[{sender_id}] "
                        target = original_mesh_msg.channel
                    
                    original_bytes = len(response_text.encode('utf-8'))
                    
                    # Reply всегда использует настройки auto_translit/force_translit из конфига
                    sent_count, total_parts, was_translit = await self._prepare_and_send_to_mesh(
                        message_text=response_text,
                        target=target,
                        is_dm=is_dm,
                        prefix=prefix,
                        force_translit=False  # Используем настройки из конфига
                    )
                    
                    if sent_count == total_parts:
                        details = []
                        if was_translit:
                            details.append("транслит")
                        if total_parts > 1:
                            details.append(f"{total_parts} частей")
                        detail_str = f" ({', '.join(details)})" if details else ""
                        self.logger.info(f"Ответ из TG отправлен{detail_str}")
                    else:
                        self.logger.warning(f"Отправлено {sent_count}/{total_parts} частей ответа")
                    return

                # Обычные сообщения → в default_channel
                if self.telegram_chat_id and chat_id_str == self.telegram_chat_id and self.default_channel:
                    text = tg_message.text or tg_message.caption or "[медиа/стикер]"
                    if text.strip():
                        sent_count, total_parts, was_translit = await self._prepare_and_send_to_mesh(
                            message_text=text,
                            target=self.default_channel,
                            is_dm=False,
                            prefix="TG: ",
                            force_translit=False  # Используем настройки из конфига
                        )
                        detail = f" (транслит)" if was_translit else ""
                        self.logger.info(
                            f"TG → default_channel #{self.default_channel} "
                            f"({sent_count}/{total_parts} частей){detail}"
                        )

            # Запуск поллинга в отдельном потоке
            def run_polling():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.tg_loop = loop
                loop.run_until_complete(self.tg_bot.polling(none_stop=True, interval=1, timeout=60))
            threading.Thread(target=run_polling, daemon=True).start()
            self.logger.info("Telegram поллинг запущен")
            
        except Exception as e:
            self.logger.error(f"Ошибка инициализации Telegram бота: {e}")
            self.enabled = False

    async def _save_reply_mapping(self, sent_msg, original_mesh_msg: MeshMessage):
        """Сохранение маппинга telegram_message_id → MeshMessage"""
        async with get_async_lock():
            key = (sent_msg.chat.id, sent_msg.message_id)
            REPLY_MAPPING[key] = original_mesh_msg

            if len(REPLY_MAPPING) > REPLY_MAPPING_MAX_MESSAGES:
                keys_to_remove = sorted(REPLY_MAPPING.keys(), key=lambda k: k[1])[:200]
                for k in keys_to_remove:
                    REPLY_MAPPING.pop(k, None)

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
            except Exception as db_e:
                self.logger.error(f"Ошибка сохранения mapping в БД: {db_e}", exc_info=True)                  

    async def _send_to_telegram_non_blocking(self, chat_id: str, text: str, original_message: MeshMessage):
        """Отправка сообщения в Telegram без блокировки."""
        if not self.tg_bot or not self.tg_loop:
            self.logger.error("Telegram бот или loop не инициализированы")
            return
            
        coro = self.tg_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=None,
            disable_web_page_preview=True
        )

        future = asyncio.run_coroutine_threadsafe(coro, self.tg_loop)
        original_mesh_msg = original_message
        
        def _handle_send_result():
            try:
                sent_msg = future.result(timeout=30)
                if sent_msg:
                    key = (sent_msg.chat.id, sent_msg.message_id)
                    if original_mesh_msg is not None:
                        with REPLY_MAPPING_LOCK_SYNC:
                            REPLY_MAPPING[key] = original_mesh_msg
                            if len(REPLY_MAPPING) > REPLY_MAPPING_MAX_MESSAGES:
                                keys_to_remove = sorted(REPLY_MAPPING.keys(), key=lambda k: k[1])[:200]
                                for k in keys_to_remove:
                                    REPLY_MAPPING.pop(k, None)

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
                            except Exception as db_e:
                                self.logger.error(f"Ошибка сохранения в БД: {db_e}", exc_info=True)

                    self.logger.info(f"Сообщение переслано в Telegram (msg_id={sent_msg.message_id})")
            except Exception as e:
                self.logger.error(f"Ошибка в _handle_send_result: {e}", exc_info=True)

        threading.Thread(target=_handle_send_result, daemon=True).start()

    async def execute(self, message: MeshMessage) -> bool:
        """Пересылка сообщений из MeshCore → Telegram"""
        if not self.should_execute(message):
            return False

        self.logger.info(f"Пересылаем сообщение от {message.sender_id} в Telegram")

        try:
            sender = message.sender_id or "Unknown"
            content = message.content.strip()

            text = f"{sender}: {content}" if self.prefix_sender else content

            if message.is_dm:
                clean_sender = sender.replace('_', '').replace('-', '').replace(' ', '')
                channel_tag = f"#{clean_sender}"
                channel_info = f" (#DM {channel_tag})"
            else:
                if message.channel:
                    channel_name = message.channel.strip()
                    clean_channel = channel_name.replace('_', '').replace('-', '').replace(' ', '')
                    if not clean_channel.startswith('#'):
                        clean_channel = f"#{clean_channel}"
                    channel_info = f" {clean_channel}"
                else:
                    channel_info = " #unknown"

            full_text = f"{text}{channel_info}"

            asyncio.create_task(
                self._send_to_telegram_non_blocking(
                    chat_id=self.telegram_chat_id,
                    text=full_text,
                    original_message=message
                )
            )

        except Exception as e:
            self.logger.error(f"Ошибка пересылки в Telegram: {e}", exc_info=True)

        return False