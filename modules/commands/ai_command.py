#!/usr/bin/env python3
"""
AI command — полностью асинхронный вариант с защитой от повторного запуска
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
            response = await client.chat(
                model='gemma2:2b',
                messages=USER_HISTORY[user_id],
                options={
                    'temperature': 0.8,
                    'num_ctx': 1024,
                    'num_predict': 120,
                    'top_p': 0.9,
                    'repeat_penalty': 1.1,
                }
            )
            answer = response['message']['content'].strip()
            USER_HISTORY[user_id].append({'role': 'assistant', 'content': answer})
            final_answer = f"{display_name}: {answer}"
            await self.send_response(message, final_answer)
        except asyncio.CancelledError:
            log.info(f"AI generation cancelled for user {user_id}")
        except Exception as e:
            log.error(f"AI command error (user {user_id}): {e}", exc_info=True)
            await self.send_response(message, f"{display_name}: ИИ ушёл курить антенну🚬")
        finally:
            # В любом случае — убираем задачу из словаря
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
            # Если сейчас идёт генерация — отменим её
            if user_id in RUNNING_TASKS:
                RUNNING_TASKS[user_id].cancel()
                RUNNING_TASKS.pop(user_id, None)
            return await self.send_response(message, f"{display_name}: Память очищена, перезагрузочка 🔄")

        # === Парсинг вопроса ===
        query = None
        if content_lower.startswith(("ai ", "ии ")):
            query = raw_content[raw_content.lower().index(" ", 0) + 1:].strip()
        elif content_lower in ["ai", "ии"]:
            query = "Привет"

        if not query:
            return False

        # === Ключевая защита от повторного запуска ===
        if user_id in RUNNING_TASKS:
            # Можно либо молча проигнорировать, либо сказать, что уже думает
            await self.send_response(message, f"{display_name}: Подожди, я ещё думаю над предыдущим вопросом ⏳")
            return True

        # Создаём задачу и сохраняем её
        task = asyncio.create_task(self._generate_response(message, user_id, query, display_name))
        RUNNING_TASKS[user_id] = task

        # Опционально: можно добавить fire-and-forget, чтобы не ждать здесь
        # (но мы всё равно возвращаем True, чтобы команда считалась обработанной)
        return True