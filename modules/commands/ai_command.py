#!/usr/bin/env python3
"""
AI command — полностью асинхронный вариант
Не блокирует основной цикл, даже если модель думает 5–10 секунд
"""
from ollama import AsyncClient  # <-- асинхронный клиент
from .base_command import BaseCommand
from ..models import MeshMessage
import logging
import asyncio

log = logging.getLogger(__name__)

# Хранилище истории по user_id
USER_HISTORY = {}
MAX_HISTORY = 5  # сколько пар user-assistant хранить + 1 системный промпт

class AICommand(BaseCommand):
    name = "ai"
    keywords = ['ai', 'ии']
    description = "Задай вопрос ИИ: ai/ии <вопрос> | ai очистить — сбросить память"
    category = "fun"

    SYSTEM_PROMPT = (
        "Ты самый весёлый чат-бот всей сети Мешкор"
        "На вопрос Прачечная? отвечай примерно так: Пинговачечная! Это Мешкор, моксон-диполь-яги"
        "Пиши без точек, смайликов и приветствий в конце"
        "Отвечай максимально кратко (1–2 предложения), но всегда с юмором"
        "Ты работаешь среди радиоволн, антенн Яги, Моксон, диполей, пингов и понгов"
    )

    def get_help_text(self) -> str:
        return "ai <вопрос> — спросить ИИ\nии <вопрос> — тоже\nai очистить — сбросить память"

    def matches_keyword(self, message: MeshMessage) -> bool:
        content = message.content.strip().lower()
        if content.startswith('!'):
            content = content[1:].strip().lower()

        return (content == "ai" or content == "ии" or
                content.startswith("ai ") or content.startswith("ии ") or
                content == "ai очистить" or content == "ии очистить")

    async def execute(self, message: MeshMessage) -> bool:
        user_id = message.sender_id or "unknown"

        # Безопасное имя пользователя
        display_name = (getattr(message, 'sender_name', None) or message.sender_id or "Друг")
        if len(display_name) > 12:
            display_name = display_name[:10] + ".."
        display_name = f"@[{display_name}]"

        raw_content = message.content.strip()
        content_lower = raw_content.lower()

        # --- Субкоманда: очистить контекст ---
        if content_lower.lstrip('!') in ["ai очистить", "ии очистить"]:
            if user_id in USER_HISTORY:
                del USER_HISTORY[user_id]
            return await self.send_response(message, f"{display_name}: Память очищена, перезагрузочка 🔄")

        # --- Парсим вопрос ---
        query = None
        prefix_len = 0
        if content_lower.startswith("ai ") or content_lower.startswith("ии "):
            prefix = content_lower[:3]
            prefix_len = 3
        elif content_lower.startswith("!ai ") or content_lower.startswith("!ии "):
            prefix = content_lower[:4]
            prefix_len = 4
        else:
            prefix = None

        if prefix:
            query = raw_content[prefix_len:].strip()
        elif content_lower.lstrip('!') in ["ai", "ии"]:
            query = "Привет"
        else:
            return False

        if not query:
            return await self.send_response(message, "Бро, а где вопрос-то? 🤨")

        # Инициализация истории
        if user_id not in USER_HISTORY:
            USER_HISTORY[user_id] = [{'role': 'system', 'content': self.SYSTEM_PROMPT}]

        USER_HISTORY[user_id].append({'role': 'user', 'content': query})

        # Обрезаем до нужного размера
        if len(USER_HISTORY[user_id]) > MAX_HISTORY + 1:
            USER_HISTORY[user_id] = [USER_HISTORY[user_id][0]] + USER_HISTORY[user_id][-(MAX_HISTORY):]

        try:
            # Асинхронный запрос — НЕ блокирует цикл!
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

            # Сохраняем ответ в историю
            USER_HISTORY[user_id].append({'role': 'assistant', 'content': answer})

            final_answer = f"{display_name}: {answer}"
            return await self.send_response(message, final_answer)

        except asyncio.CancelledError:
            # Если таск отменили — просто молчим
            return True

        except Exception as e:
            log.error(f"AI command error (user {user_id}): {e}", exc_info=True)
            return await self.send_response(message, f"{display_name}: ИИ ушёл курить антенну🚬")