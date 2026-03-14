#!/usr/bin/env python3
"""
AI command — полностью асинхронный вариант с защитой от повторного запуска
+ Поддержка бэкендов: ollama (локальная модель) и openrouter (OpenAI-совместимый API)
+ Отправка ответов ИИ теперь использует логику TelegramBridgeCommand
  (разбивка на части, транслитерация, паузы, нумерация частей)
+ Все настройки разбивки/транслитерации берутся из [Telegram_Bridge]
+ Если TelegramBridge плагин не загружен или отключён — fallback к простой отправке
"""

import json
import aiohttp
from .base_command import BaseCommand
from ..models import MeshMessage
import logging
import asyncio

log = logging.getLogger(__name__)

# Хранилище истории по user_id
USER_HISTORY = {}
# Задачи, которые сейчас выполняются (по user_id → asyncio.Task)
RUNNING_TASKS: dict[str, asyncio.Task] = {}

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
        "Ты работаешь среди радиоволн, антенн Яги, Моксон, диполей, Гало, "
        "децибелов, адвертов, пингов и понгов."
    )

    # ------------------------------------------------------------------ init
    def __init__(self, bot):
        super().__init__(bot)
        self.system_prompt = self._load_system_prompt()

        # ---------- выбор бэкенда ----------
        self.backend = (
            self.bot.config
            .get('AI_Command', 'backend', fallback='ollama')
            .strip().lower()
        )

        if self.backend == 'openrouter':
            self._init_openrouter()
        else:
            self._init_ollama()

    # ------------------------------------------------- инициализация Ollama
    def _init_ollama(self):
        self.model = self.bot.config.get(
            'AI_Command', 'model', fallback='gemma2:2b'
        ).strip()
        self.logger.info(f"AI backend: ollama | model: {self.model}")

    # --------------------------------------------- инициализация OpenRouter
    def _init_openrouter(self):
        self.api_key = self.bot.config.get(
            'AI_Command', 'openrouter_api_key', fallback=''
        ).strip()
        self.api_base_url = self.bot.config.get(
            'AI_Command', 'openrouter_base_url',
            fallback='https://openrouter.ai/api/v1'
        ).strip().rstrip('/')
        self.model = self.bot.config.get(
            'AI_Command', 'openrouter_model',
            fallback='google/gemma-2-2b-it'
        ).strip()
        self.api_timeout = self.bot.config.getint(
            'AI_Command', 'openrouter_timeout', fallback=60
        )

        if not self.api_key:
            self.logger.error(
                "OpenRouter API key не задан! "
                "Добавьте openrouter_api_key в секцию [AI_Command]"
            )

        self.logger.info(
            f"AI backend: openrouter | model: {self.model} | "
            f"url: {self.api_base_url}"
        )

    # --------------------------------------------- загрузка системного промпта
    def _load_system_prompt(self):
        prompt_file = self.bot.config.get(
            'AI_Command', 'system_prompt_file', fallback=None
        )

        if prompt_file and prompt_file.strip():
            prompt_file = prompt_file.strip()
            try:
                with open(prompt_file, 'r', encoding='utf-8') as f:
                    prompt = f.read().strip()
                if prompt:
                    self.logger.info(
                        f"Загружен системный промпт из файла {prompt_file}"
                    )
                    return prompt
                else:
                    self.logger.warning(
                        f"Файл системного промпта пустой: {prompt_file}"
                    )
            except FileNotFoundError:
                self.logger.warning(
                    f"Файл системного промпта не найден: {prompt_file}"
                )
            except Exception as e:
                self.logger.warning(
                    f"Ошибка чтения файла промпта {prompt_file}: {e}"
                )

        self.logger.info("Используется стандартный системный промпт")
        return self.DEFAULT_SYSTEM_PROMPT

    # ------------------------------------------------------------------ help
    def get_help_text(self) -> str:
        return (
            "ai <вопрос> — спросить ИИ\n"
            "ии <вопрос> — тоже\n"
            "ai очистить — сбросить память"
        )

    # -------------------------------------------------------- matches_keyword
    def matches_keyword(self, message: MeshMessage) -> bool:
        content = message.content.strip().lower()
        if content.startswith('!'):
            content = content[1:].strip().lower()
        return (
            content in ("ai", "ии", "ai очистить", "ии очистить")
            or content.startswith("ai ")
            or content.startswith("ии ")
        )

    # ======================================================================
    #                       БЭКЕНДЫ ГЕНЕРАЦИИ
    # ======================================================================

    async def _chat(self, messages: list[dict]) -> str:
        """Единая точка входа — маршрутизация по бэкенду."""
        if self.backend == 'openrouter':
            return await self._chat_openrouter(messages)
        return await self._chat_ollama(messages)

    # -------------------------------------------------- Ollama
    async def _chat_ollama(self, messages: list[dict]) -> str:
        """Генерация ответа через локальную Ollama."""
        try:
            from ollama import AsyncClient
        except ImportError:
            raise RuntimeError(
                "Пакет 'ollama' не установлен. "
                "pip install ollama  — или переключитесь на backend = openrouter"
            )

        client = AsyncClient()
        response = await client.chat(
            model=self.model,
            messages=messages,
            options={
                'temperature':    self.bot.config.getfloat(
                    'AI_Command', 'temperature', fallback=0.85),
                'num_ctx':        self.bot.config.getint(
                    'AI_Command', 'num_ctx', fallback=1024),
                'num_predict':    self.bot.config.getint(
                    'AI_Command', 'num_predict', fallback=120),
                'top_p':          self.bot.config.getfloat(
                    'AI_Command', 'top_p', fallback=0.92),
                'repeat_penalty': self.bot.config.getfloat(
                    'AI_Command', 'repeat_penalty', fallback=1.12),
            },
        )
        return response['message']['content'].strip()

    # -------------------------------------------------- OpenRouter / OpenAI-compatible
    async def _chat_openrouter(self, messages: list[dict]) -> str:
        """Генерация ответа через OpenRouter (OpenAI-совместимый API)."""
        if not self.api_key:
            raise RuntimeError("OpenRouter API key не сконфигурирован")

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type':  'application/json',
        }
        referer = self.bot.config.get(
            'AI_Command', 'openrouter_referer', fallback=''
        ).strip()
        title = self.bot.config.get(
            'AI_Command', 'openrouter_title', fallback='MeshBot'
        ).strip()
        if referer:
            headers['HTTP-Referer'] = referer
        if title:
            headers['X-Title'] = title

        temperature = self.bot.config.getfloat(
            'AI_Command', 'temperature', fallback=0.85
        )
        top_p = self.bot.config.getfloat(
            'AI_Command', 'top_p', fallback=0.92
        )

        num_predict = self.bot.config.getint(
            'AI_Command', 'num_predict', fallback=120
        )
        max_tokens = self.bot.config.getint(
            'AI_Command', 'max_tokens', fallback=num_predict
        )
        max_completion_tokens = self.bot.config.getint(
            'AI_Command', 'max_completion_tokens', fallback=max_tokens * 4
        )
        reasoning_effort = self.bot.config.get(
            'AI_Command', 'reasoning_effort', fallback='low'
        ).strip().lower()

        default_freq = (
            self.bot.config.getfloat(
                'AI_Command', 'repeat_penalty', fallback=1.12
            ) - 1.0
        )
        frequency_penalty = self.bot.config.getfloat(
            'AI_Command', 'frequency_penalty', fallback=default_freq
        )

        body = {
            'model':                 self.model,
            'messages':              messages,
            'temperature':           temperature,
            'top_p':                 top_p,
            'max_tokens':            max_tokens,
            'max_completion_tokens': max_completion_tokens,
            'frequency_penalty':     frequency_penalty,
        }

        if reasoning_effort in ('low', 'medium', 'high'):
            body['reasoning'] = {'effort': reasoning_effort}

        url = f"{self.api_base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.api_timeout)

        self.logger.debug(
            f"OpenRouter → {url} | model={self.model} | "
            f"max_tok={max_tokens} max_compl={max_completion_tokens} "
            f"reasoning_effort={reasoning_effort}"
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                resp_text = await resp.text()

                if resp.status != 200:
                    self.logger.error(
                        f"OpenRouter API {resp.status}: {resp_text[:500]}"
                    )
                    raise RuntimeError(
                        f"OpenRouter API error {resp.status}: {resp_text[:200]}"
                    )

                try:
                    data = json.loads(resp_text)
                except json.JSONDecodeError:
                    self.logger.error(
                        f"OpenRouter не-JSON: {resp_text[:500]}"
                    )
                    raise RuntimeError("OpenRouter вернул невалидный JSON")

        # --- разбор ответа ---
        try:
            choices = data.get('choices')
            if not choices:
                self.logger.error(
                    f"OpenRouter пустой choices: "
                    f"{json.dumps(data, ensure_ascii=False)[:500]}"
                )
                raise RuntimeError("OpenRouter вернул пустой choices")

            choice = choices[0]
            msg = choice.get('message', {})
            content   = msg.get('content')
            reasoning = msg.get('reasoning_content') or msg.get('reasoning')
            reasoning_details = msg.get('reasoning_details')
            finish_reason = choice.get('finish_reason', 'unknown')

            has_content   = content is not None and content.strip() != ""
            has_reasoning = reasoning is not None and str(reasoning).strip() != ""
            has_encrypted = (
                isinstance(reasoning_details, list)
                and len(reasoning_details) > 0
                and any(
                    d.get("type", "").startswith("reasoning.encrypted")
                    for d in reasoning_details
                    if isinstance(d, dict)
                )
            )

            # 1) Обычный content
            if has_content:
                answer = content.strip()
            # 2) Открытый reasoning
            elif has_reasoning and not has_encrypted:
                answer = self._extract_from_reasoning(str(reasoning).strip())
                self.logger.info("Ответ извлечён из reasoning")
            # 3) Зашифрованный reasoning + finish=length
            elif has_encrypted and finish_reason == "length":
                self.logger.warning(
                    f"Reasoning зашифрован, все токены ушли на reasoning. "
                    f"model={data.get('model')} "
                    f"max_completion_tokens={max_completion_tokens}"
                )
                answer = "ИИ слишком глубоко задумался, попробуй ещё раз 🤔"
            # 4) Refusal
            elif msg.get("refusal"):
                self.logger.warning(f"OpenRouter refusal: {msg['refusal']}")
                answer = "Модель отказалась отвечать 🤷"
            # 5) Неизвестная ситуация
            else:
                self.logger.warning(
                    f"OpenRouter content=null | finish={finish_reason} | "
                    f"keys={list(msg.keys())} | "
                    f"resp: {json.dumps(data, ensure_ascii=False)[:500]}"
                )
                answer = "ИИ задумался и ничего не сказал 🤔"

            if not answer or not answer.strip():
                answer = "ИИ ответил пустотой 🫥"

        except (KeyError, IndexError, TypeError) as exc:
            self.logger.error(
                f"Неожиданный формат OpenRouter: "
                f"{json.dumps(data, ensure_ascii=False)[:500]}"
            )
            raise RuntimeError(f"Неожиданный формат ответа: {exc}")

        # usage
        usage = data.get('usage')
        if usage:
            ct_details = usage.get('completion_tokens_details', {})
            rt = ct_details.get('reasoning_tokens', 0)
            extra = f" reasoning={rt}" if rt else ""
            self.logger.debug(
                f"OpenRouter usage: prompt={usage.get('prompt_tokens')}, "
                f"completion={usage.get('completion_tokens')}, "
                f"total={usage.get('total_tokens')}{extra}"
            )

        return answer

    @staticmethod
    def _extract_from_reasoning(reasoning: str) -> str:
        """Извлекает финальный ответ из reasoning_content."""
        markers = ["ответ:", "answer:", "итого:", "итог:", "вывод:",
                    "result:", "**ответ", "**answer", "final answer"]
        lower = reasoning.lower()
        for marker in markers:
            pos = lower.rfind(marker)
            if pos != -1:
                after = reasoning[pos + len(marker):].strip().lstrip(":*").strip()
                if after:
                    return after
        paragraphs = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
        if paragraphs:
            return paragraphs[-1]
        return reasoning

    # ======================================================================
    #                        ГЕНЕРАЦИЯ + ОТПРАВКА
    # ======================================================================

    async def _generate_response(
        self,
        message: MeshMessage,
        user_id: str,
        query: str,
        display_name: str,
    ):
        """Корутина — генерация ответа и отправка в меш."""
        try:
            # Инициализация истории
            if user_id not in USER_HISTORY:
                USER_HISTORY[user_id] = [
                    {'role': 'system', 'content': self.system_prompt}
                ]

            USER_HISTORY[user_id].append({'role': 'user', 'content': query})

            # Обрезаем историю (system + последние MAX_HISTORY сообщений)
            if len(USER_HISTORY[user_id]) > MAX_HISTORY + 1:
                USER_HISTORY[user_id] = (
                    [USER_HISTORY[user_id][0]]
                    + USER_HISTORY[user_id][-(MAX_HISTORY):]
                )

            # ---------- вызов бэкенда ----------
            answer = await self._chat(USER_HISTORY[user_id])
            USER_HISTORY[user_id].append(
                {'role': 'assistant', 'content': answer}
            )

            # === ОТПРАВКА ОТВЕТА ===
            telegram_bridge = (
                self.bot.command_manager.get_plugin_by_name('telegram_bridge')
            )

            if telegram_bridge and hasattr(
                telegram_bridge, '_prepare_and_send_to_mesh'
            ):
                target = (
                    message.sender_id if message.is_dm else message.channel
                )
                is_dm = message.is_dm
                prefix = f"{display_name}: "

                self.logger.debug(
                    "Используем TelegramBridge для отправки AI-ответа "
                    "(разбивка + транслит)"
                )
                sent_count, total_parts, was_translit = (
                    await telegram_bridge._prepare_and_send_to_mesh(
                        message_text=answer,
                        target=target,
                        is_dm=is_dm,
                        prefix=prefix,
                        force_translit=False,
                    )
                )

                self.logger.info(
                    f"AI ответ отправлен через TelegramBridge: "
                    f"{sent_count}/{total_parts} частей"
                    f"{' (транслит)' if was_translit else ''}"
                )
            else:
                self.logger.debug(
                    "TelegramBridge недоступен — fallback к простой отправке"
                )
                await self.send_response(
                    message, f"{display_name}: {answer}"
                )

        except asyncio.CancelledError:
            log.info(f"AI generation cancelled for user {user_id}")
        except Exception as e:
            log.error(
                f"AI command error (user {user_id}): {e}", exc_info=True
            )
            await self.send_response(
                message, f"{display_name}: ИИ вышел покурить🚬"
            )
        finally:
            RUNNING_TASKS.pop(user_id, None)

    # ======================================================================
    #                             EXECUTE
    # ======================================================================

    async def execute(self, message: MeshMessage) -> bool:
        user_id = str(message.sender_id or "unknown")
        display_name = (
            getattr(message, 'sender_name', None)
            or message.sender_id
            or "Друг"
        )
        if len(display_name) > 12:
            display_name = display_name[:10] + ".."
        display_name = f"@[{display_name}]"

        raw_content = message.content.strip()
        content_lower = raw_content.lower().lstrip('!')

        # === Очистка памяти ===
        if content_lower in ("ai очистить", "ии очистить"):
            if user_id in USER_HISTORY:
                del USER_HISTORY[user_id]
            if user_id in RUNNING_TASKS:
                RUNNING_TASKS[user_id].cancel()
                RUNNING_TASKS.pop(user_id, None)
            return await self.send_response(
                message,
                f"{display_name}: Память очищена, перезагрузочка 🔄",
            )

        # === Парсинг вопроса ===
        query = None
        if content_lower.startswith(("ai ", "ии ")):
            query = raw_content[raw_content.lower().find(" ", 0) + 1:].strip()
        elif content_lower in ("ai", "ии"):
            query = "Привет"

        if not query:
            return False

        # === Защита от повторного запуска ===
        if user_id in RUNNING_TASKS:
            await self.send_response(
                message,
                f"{display_name}: Подожди, я ещё думаю "
                f"над предыдущим вопросом ⏳",
            )
            return True

        # Создаём задачу
        task = asyncio.create_task(
            self._generate_response(message, user_id, query, display_name)
        )
        RUNNING_TASKS[user_id] = task
        return True