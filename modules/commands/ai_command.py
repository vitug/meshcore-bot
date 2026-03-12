#!/usr/bin/env python3
"""
AI command — полностью асинхронный вариант с защитой от повторного запуска
+ Отправка ответов ИИ теперь использует логику TelegramBridgeCommand
  (разбивка на части, транслитерация, паузы, нумерация частей)
+ Все настройки разбивки/транслитерации берутся из [Telegram_Bridge]
+ Если TelegramBridge плагин не загружен или отключён — fallback к простой отправке
"""
from ollama import AsyncClient
from .base_command import BaseCommand
from ..models import MeshMessage
import logging
import asyncio

log = logging.getLogger(__name__)

# Хранилище истории по user_id
USER_HISTORY = {}

# Задачи, которые сейчас выполняются (по user_id → asyncio.Task)
RUNNING_TASKS: dict[str, asyncio.Task] = {}

# Опционально: можно ещё и Lock на пользователя, но Task достаточно
MAX_HISTORY = 7  # сколько пар user-assistant хранить + 1 системный промпт


class AICommand(BaseCommand):
    name = "ai"
    keywords = ['ai', 'ии']
    description = "Задай вопрос ИИ: ai/ии <вопрос> | ai очистить — сбросить память"
    category = "fun"

    DEFAULT_SYSTEM_PROMPT = (
        "Ты самый весёлый чат-бот всей сети Мешкор. "
        "Пиши без лишних точек, смайликов и приветствий. "
        "Отвечай максимально кратко (1–2 предложения), но всегда с юмором для технарей. "
        "Ты работаешь среди радиоволн, антенн Яги, Моксон, диполей, Гало, децибелов, адвертов, пингов и понгов."
    )

    def __init__(self, bot):
        super().__init__(bot)
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self):
        prompt_file = self.bot.config.get('AI_Command', 'system_prompt_file', fallback=None)
        
        if prompt_file and prompt_file.strip():
            prompt_file = prompt_file.strip()
            try:
                with open(prompt_file, 'r', encoding='utf-8') as f:
                    prompt = f.read().strip()
                if prompt:
                    self.logger.info(f"Загружен системный промпт из файла {prompt_file}")
                    return prompt
                else:
                    self.logger.warning(f"Файл системного промпта пустой: {prompt_file}")
            except FileNotFoundError:
                self.logger.warning(f"Файл системного промпта не найден: {prompt_file}")
            except Exception as e:
                self.logger.warning(f"Ошибка чтения файла системного промпта {prompt_file}: {e}")
        
        self.logger.info("Используется стандартный системный промпт")
        return self.DEFAULT_SYSTEM_PROMPT

    def get_help_text(self) -> str:
        return "ai <вопрос> — спросить ИИ\nии <вопрос> — тоже\nai очистить — сбросить память"

    def matches_keyword(self, message: MeshMessage) -> bool:
        content = message.content.strip().lower()
        if content.startswith('!'):
            content = content[1:].strip().lower()
        return (content == "ai" or content == "ии" or
                content.startswith("ai ") or content.startswith("ии ") or
                content == "ai очистить" or content == "ии очистить")

    async def _generate_response(self, message: MeshMessage, user_id: str, query: str, display_name: str):
        """Отдельная корутина — сюда вынесена вся логика генерации"""
        try:
            # Инициализация истории
            if user_id not in USER_HISTORY:
                USER_HISTORY[user_id] = [{'role': 'system', 'content': self.system_prompt}]
            USER_HISTORY[user_id].append({'role': 'user', 'content': query})
            # Обрезаем историю
            if len(USER_HISTORY[user_id]) > MAX_HISTORY + 1:
                USER_HISTORY[user_id] = [USER_HISTORY[user_id][0]] + USER_HISTORY[user_id][-(MAX_HISTORY):]

            client = AsyncClient()
            model_name = self.bot.config.get('AI_Command', 'model', fallback='gemma2:2b')
            response = await client.chat(
                model = model_name,
                messages=USER_HISTORY[user_id],
                options={
                    'temperature':   self.bot.config.getfloat('AI_Command', 'temperature',   fallback=0.85),
                    'num_ctx':       self.bot.config.getint(  'AI_Command', 'num_ctx',       fallback=1024),
                    'num_predict':   self.bot.config.getint(  'AI_Command', 'num_predict',   fallback=120),
                    'top_p':         self.bot.config.getfloat('AI_Command', 'top_p',         fallback=0.92),
                    'repeat_penalty':self.bot.config.getfloat('AI_Command', 'repeat_penalty',fallback=1.12),
                }
            )
            answer = response['message']['content'].strip()
            USER_HISTORY[user_id].append({'role': 'assistant', 'content': answer})

            # === ОТПРАВКА ОТВЕТА ===
            # Пытаемся использовать TelegramBridge для разбивки/транслитерации
            telegram_bridge = self.bot.command_manager.get_plugin_by_name('telegram_bridge')
            
            if telegram_bridge and hasattr(telegram_bridge, '_prepare_and_send_to_mesh'):
                # Определяем цель и тип (DM или канал)
                target = message.sender_id if message.is_dm else message.channel
                is_dm = message.is_dm
                prefix = f"{display_name}: "

                self.logger.debug("Используем TelegramBridge для отправки AI-ответа (разбивка + транслит)")
                sent_count, total_parts, was_translit = await telegram_bridge._prepare_and_send_to_mesh(
                    message_text=answer,
                    target=target,
                    is_dm=is_dm,
                    prefix=prefix,
                    force_translit=False  # используем настройки из [Telegram_Bridge]
                )
                
                self.logger.info(
                    f"AI ответ отправлен через TelegramBridge: {sent_count}/{total_parts} частей"
                    f"{' (транслит)' if was_translit else ''}"
                )
            else:
                # Fallback — простая отправка без разбивки и транслита
                self.logger.debug("TelegramBridge не доступен — fallback к простой отправке")
                await self.send_response(message, f"{display_name}: {answer}")

        except asyncio.CancelledError:
            log.info(f"AI generation cancelled for user {user_id}")
        except Exception as e:
            log.error(f"AI command error (user {user_id}): {e}", exc_info=True)
            await self.send_response(message, f"{display_name}: ИИ вышел покурить🚬")
        finally:
            RUNNING_TASKS.pop(user_id, None)

    async def execute(self, message: MeshMessage) -> bool:
        user_id = str(message.sender_id or "unknown")
        display_name = (getattr(message, 'sender_name', None) or message.sender_id or "Друг")
        if len(display_name) > 12:
            display_name = display_name[:10] + ".."
        display_name = f"@[{display_name}]"

        raw_content = message.content.strip()
        content_lower = raw_content.lower().lstrip('!')

        # === Очистка памяти ===
        if content_lower in ["ai очистить", "ии очистить"]:
            if user_id in USER_HISTORY:
                del USER_HISTORY[user_id]
            if user_id in RUNNING_TASKS:
                RUNNING_TASKS[user_id].cancel()
                RUNNING_TASKS.pop(user_id, None)
            return await self.send_response(message, f"{display_name}: Память очищена, перезагрузочка 🔄")

        # === Парсинг вопроса ===
        query = None
        if content_lower.startswith(("ai ", "ии ")):
            query = raw_content[raw_content.lower().find(" ", 0) + 1:].strip()
        elif content_lower in ["ai", "ии"]:
            query = "Привет"

        if not query:
            return False

        # === Защита от повторного запуска ===
        if user_id in RUNNING_TASKS:
            await self.send_response(message, f"{display_name}: Подожди, я ещё думаю над предыдущим вопросом ⏳")
            return True

        # Создаём задачу
        task = asyncio.create_task(self._generate_response(message, user_id, query, display_name))
        RUNNING_TASKS[user_id] = task

        return True