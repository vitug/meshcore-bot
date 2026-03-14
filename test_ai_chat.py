#!/usr/bin/env python3
"""
Тест-скрипт для проверки подключения к AI-бэкенду (Ollama / OpenRouter).
Читает настройки из конфига бота и ведёт интерактивный чат в терминале.

Использование:
    python test_ai_chat.py                        # конфиг по умолчанию
    python test_ai_chat.py -c /path/to/config.ini
    python test_ai_chat.py --single "Привет!"     # одиночный вопрос
    python test_ai_chat.py --debug                # показывать сырой JSON
"""

import argparse
import asyncio
import configparser
import json
import sys
import os

# ─── Цвета ───
C_RESET   = "\033[0m"
C_BOLD    = "\033[1m"
C_GREEN   = "\033[32m"
C_CYAN    = "\033[36m"
C_YELLOW  = "\033[33m"
C_RED     = "\033[31m"
C_DIM     = "\033[2m"
C_MAGENTA = "\033[35m"

DEFAULT_CONFIG_PATHS = [
    "config.ini",
    "/opt/meshcore-bot/config.ini",
    os.path.expanduser("~/.meshcore-bot/config.ini"),
]

DEFAULT_SYSTEM_PROMPT = (
    "Ты самый весёлый чат-бот всей сети Мешкор. "
    "Пиши без лишних точек, смайликов и приветствий. "
    "Отвечай максимально кратко (1–2 предложения), но всегда с юмором для технарей. "
    "Ты работаешь среди радиоволн, антенн Яги, Моксон, диполей, Гало, "
    "децибелов, адвертов, пингов и понгов."
)

DEBUG = False


def find_config(explicit_path: str | None) -> str:
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        print(f"{C_RED}Конфиг не найден: {explicit_path}{C_RESET}")
        sys.exit(1)
    for p in DEFAULT_CONFIG_PATHS:
        if os.path.isfile(p):
            return p
    print(f"{C_RED}Конфиг не найден. Проверены:{C_RESET}")
    for p in DEFAULT_CONFIG_PATHS:
        print(f"  - {p}")
    sys.exit(1)


def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    if "AI_Command" not in cfg:
        print(f"{C_RED}Секция [AI_Command] не найдена в {path}{C_RESET}")
        sys.exit(1)
    return cfg


def load_system_prompt(cfg: configparser.ConfigParser) -> str:
    prompt_file = cfg.get("AI_Command", "system_prompt_file", fallback="").strip()
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                print(f"{C_DIM}Системный промпт из файла: {prompt_file}{C_RESET}")
                return text
        except Exception as e:
            print(f"{C_YELLOW}Не удалось загрузить промпт: {e}{C_RESET}")
    return DEFAULT_SYSTEM_PROMPT


def extract_from_reasoning(reasoning: str) -> str:
    """Извлекает финальный ответ из reasoning_content / reasoning."""
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
#                           Ollama
# ======================================================================

async def chat_ollama(cfg: configparser.ConfigParser, messages: list[dict]) -> str:
    try:
        from ollama import AsyncClient
    except ImportError:
        print(f"{C_RED}pip install ollama{C_RESET}")
        sys.exit(1)

    model = cfg.get("AI_Command", "model", fallback="gemma2:2b").strip()
    client = AsyncClient()

    response = await client.chat(
        model=model,
        messages=messages,
        options={
            "temperature":    cfg.getfloat("AI_Command", "temperature",    fallback=0.85),
            "num_ctx":        cfg.getint("AI_Command",   "num_ctx",        fallback=1024),
            "num_predict":    cfg.getint("AI_Command",   "num_predict",    fallback=120),
            "top_p":          cfg.getfloat("AI_Command", "top_p",          fallback=0.92),
            "repeat_penalty": cfg.getfloat("AI_Command", "repeat_penalty", fallback=1.12),
        },
    )

    if DEBUG:
        print(f"\n{C_DIM}  RAW ollama:{C_RESET}")
        print(json.dumps(response, ensure_ascii=False, indent=2, default=str)[:2000])

    content = response["message"]["content"]
    return content.strip() if content else "[пустой ответ]"


# ======================================================================
#                         OpenRouter
# ======================================================================

async def chat_openrouter(cfg: configparser.ConfigParser, messages: list[dict]) -> str:
    import aiohttp

    api_key = cfg.get("AI_Command", "openrouter_api_key", fallback="").strip()
    if not api_key:
        print(f"{C_RED}openrouter_api_key не задан!{C_RESET}")
        sys.exit(1)

    base_url = cfg.get(
        "AI_Command", "openrouter_base_url",
        fallback="https://openrouter.ai/api/v1",
    ).strip().rstrip("/")
    model = cfg.get(
        "AI_Command", "openrouter_model",
        fallback="google/gemma-2-2b-it",
    ).strip()
    timeout_sec = cfg.getint("AI_Command", "openrouter_timeout", fallback=60)

    temperature = cfg.getfloat("AI_Command", "temperature", fallback=0.85)
    top_p       = cfg.getfloat("AI_Command", "top_p",       fallback=0.92)

    # --- Бюджет токенов ---
    # Для reasoning-моделей max_completion_tokens = reasoning + content
    # Для обычных моделей max_tokens = только content
    num_predict = cfg.getint("AI_Command", "num_predict", fallback=120)
    max_tokens  = cfg.getint("AI_Command", "max_tokens", fallback=num_predict)

    # max_completion_tokens — бюджет для reasoning-моделей (reasoning + content)
    # По умолчанию = max_tokens * 4, чтобы хватило на reasoning
    max_completion_tokens = cfg.getint(
        "AI_Command", "max_completion_tokens", fallback=max_tokens * 4
    )

    # reasoning effort: low / medium / high (пустая строка = не отправлять)
    reasoning_effort = cfg.get(
        "AI_Command", "reasoning_effort", fallback="low"
    ).strip().lower()

    default_freq = cfg.getfloat("AI_Command", "repeat_penalty", fallback=1.12) - 1.0
    frequency_penalty = cfg.getfloat("AI_Command", "frequency_penalty", fallback=default_freq)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    referer = cfg.get("AI_Command", "openrouter_referer", fallback="").strip()
    title   = cfg.get("AI_Command", "openrouter_title",   fallback="MeshBot-Test").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title

    body = {
        "model":                  model,
        "messages":               messages,
        "temperature":            temperature,
        "top_p":                  top_p,
        "max_tokens":             max_tokens,
        "max_completion_tokens":  max_completion_tokens,
        "frequency_penalty":      frequency_penalty,
    }

    # Добавляем reasoning effort если указан
    if reasoning_effort in ("low", "medium", "high"):
        body["reasoning"] = {"effort": reasoning_effort}

    url = f"{base_url}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    if DEBUG:
        print(f"\n{C_DIM}  REQUEST body:{C_RESET}")
        print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            resp_text = await resp.text()

            if resp.status != 200:
                print(f"{C_RED}OpenRouter HTTP {resp.status}:{C_RESET}")
                print(resp_text[:1000])
                raise RuntimeError(f"HTTP {resp.status}")

            data = json.loads(resp_text)

    if DEBUG:
        print(f"\n{C_DIM}  RAW OpenRouter response:{C_RESET}")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        print()

    # --- разбор ответа ---
    choices = data.get("choices")
    if not choices:
        raise RuntimeError(f"Пустой choices: {json.dumps(data, ensure_ascii=False)[:500]}")

    choice = choices[0]
    msg = choice.get("message", {})
    content   = msg.get("content")
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    reasoning_details = msg.get("reasoning_details")
    finish = choice.get("finish_reason", "?")

    # Детали по токенам
    usage = data.get("usage", {})
    completion_details = usage.get("completion_tokens_details", {})
    reasoning_tokens = completion_details.get("reasoning_tokens", 0)
    total_completion = usage.get("completion_tokens", 0)

    has_content   = content is not None and content.strip() != ""
    has_reasoning = reasoning is not None and str(reasoning).strip() != ""
    has_encrypted = (
        reasoning_details is not None
        and isinstance(reasoning_details, list)
        and len(reasoning_details) > 0
        and any(d.get("type", "").startswith("reasoning.encrypted")
                for d in reasoning_details if isinstance(d, dict))
    )

    # Диагностика
    if has_encrypted:
        print(f"{C_MAGENTA}  ⚠ Reasoning зашифрован (encrypted), "
              f"{reasoning_tokens} reasoning-токенов из {total_completion} completion{C_RESET}")
    elif has_reasoning:
        print(f"{C_MAGENTA}  [reasoning получен, {len(str(reasoning))} символов]{C_RESET}")

    # 1) Обычный content
    if has_content:
        answer = content.strip()
    # 2) Открытый reasoning (можно прочитать)
    elif has_reasoning and not has_encrypted:
        answer = extract_from_reasoning(str(reasoning).strip())
        print(f"{C_MAGENTA}  → Ответ извлечён из reasoning{C_RESET}")
    # 3) Зашифрованный reasoning, content=null, finish=length
    #    Модель потратила все токены на reasoning → content не сгенерирован
    elif has_encrypted and finish == "length":
        answer = (
            f"⚠ Модель ({data.get('model', '?')}) потратила все "
            f"{total_completion} токенов на reasoning и не успела ответить. "
            f"Увеличьте max_completion_tokens (сейчас {max_completion_tokens}) "
            f"или задайте reasoning_effort = low в конфиге."
        )
    # 4) Refusal
    elif msg.get("refusal"):
        answer = f"[refusal] {msg['refusal']}"
    # 5) Неизвестная ситуация
    else:
        msg_keys = list(msg.keys())
        answer = (f"[content=null, finish_reason={finish}, "
                  f"message_keys={msg_keys}]")

    # usage
    if usage:
        pt = usage.get("prompt_tokens", "?")
        ct = usage.get("completion_tokens", "?")
        tt = usage.get("total_tokens", "?")
        rt = reasoning_tokens or 0
        info = f"  tokens: prompt={pt}  completion={ct}  total={tt}"
        if rt:
            info += f"  (reasoning={rt})"
        print(f"{C_DIM}{info}{C_RESET}")

    return answer


# ======================================================================

async def chat(cfg: configparser.ConfigParser, messages: list[dict]) -> str:
    backend = cfg.get("AI_Command", "backend", fallback="ollama").strip().lower()
    if backend == "openrouter":
        return await chat_openrouter(cfg, messages)
    return await chat_ollama(cfg, messages)


# ======================================================================

async def interactive_loop(cfg: configparser.ConfigParser, single_query: str | None):
    global DEBUG

    backend = cfg.get("AI_Command", "backend", fallback="ollama").strip().lower()
    if backend == "openrouter":
        model = cfg.get("AI_Command", "openrouter_model",
                        fallback="google/gemma-2-2b-it").strip()
    else:
        model = cfg.get("AI_Command", "model", fallback="gemma2:2b").strip()

    system_prompt = load_system_prompt(cfg)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    max_history = 7

    # Показываем настройки
    max_tok = cfg.getint("AI_Command", "max_tokens",
                         fallback=cfg.getint("AI_Command", "num_predict", fallback=120))
    max_compl = cfg.getint("AI_Command", "max_completion_tokens", fallback=max_tok * 4)
    r_effort = cfg.get("AI_Command", "reasoning_effort", fallback="low").strip()

    print()
    print(f"{C_BOLD}╔══════════════════════════════════════════╗{C_RESET}")
    print(f"{C_BOLD}║   AI Chat Test                            ║{C_RESET}")
    print(f"{C_BOLD}╠══════════════════════════════════════════╣{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} Backend              : {C_CYAN}{backend}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} Model                : {C_CYAN}{model}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} Temperature          : {C_CYAN}"
          f"{cfg.getfloat('AI_Command', 'temperature', fallback=0.85)}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} max_tokens           : {C_CYAN}{max_tok}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} max_completion_tokens: {C_CYAN}{max_compl}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} reasoning_effort     : {C_CYAN}{r_effort}{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} Debug                : {C_CYAN}{'ON' if DEBUG else 'OFF'}{C_RESET}")
    print(f"{C_BOLD}╠══════════════════════════════════════════╣{C_RESET}")
    print(f"{C_BOLD}║{C_RESET} {C_YELLOW}/clear{C_RESET}   — очистить историю")
    print(f"{C_BOLD}║{C_RESET} {C_YELLOW}/history{C_RESET} — показать историю")
    print(f"{C_BOLD}║{C_RESET} {C_YELLOW}/debug{C_RESET}   — вкл/выкл сырой JSON")
    print(f"{C_BOLD}║{C_RESET} {C_YELLOW}/quit{C_RESET}    — выйти")
    print(f"{C_BOLD}╚══════════════════════════════════════════╝{C_RESET}")
    print()

    if single_query:
        print(f"{C_GREEN}Вы:{C_RESET} {single_query}")
        messages.append({"role": "user", "content": single_query})
        try:
            answer = await chat(cfg, messages)
            print(f"{C_CYAN}ИИ:{C_RESET} {answer}")
        except Exception as e:
            print(f"{C_RED}Ошибка:{C_RESET} {e}")
        return

    while True:
        try:
            user_input = input(f"\n{C_GREEN}Вы: {C_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C_YELLOW}Выход.{C_RESET}")
            break

        if not user_input:
            continue

        if user_input.lower() in ("/quit", "/exit", "/q"):
            print(f"{C_YELLOW}Выход.{C_RESET}")
            break

        if user_input.lower() in ("/clear", "/очистить"):
            messages = [{"role": "system", "content": system_prompt}]
            print(f"{C_YELLOW}История очищена.{C_RESET}")
            continue

        if user_input.lower() == "/debug":
            DEBUG = not DEBUG
            print(f"{C_YELLOW}Debug: {'ON' if DEBUG else 'OFF'}{C_RESET}")
            continue

        if user_input.lower() in ("/history", "/история"):
            print(f"\n{C_DIM}--- История ({len(messages)}) ---{C_RESET}")
            for i, m in enumerate(messages):
                role = m["role"]
                text = m["content"][:120] + ("…" if len(m["content"]) > 120 else "")
                color = {"system": C_DIM, "user": C_GREEN,
                         "assistant": C_CYAN}.get(role, "")
                print(f"  {color}[{i}] {role}: {text}{C_RESET}")
            print(f"{C_DIM}--- конец ---{C_RESET}")
            continue

        messages.append({"role": "user", "content": user_input})
        if len(messages) > max_history + 1:
            messages = [messages[0]] + messages[-(max_history):]

        try:
            print(f"{C_DIM}  думаю...{C_RESET}", end="", flush=True)
            answer = await chat(cfg, messages)
            print(f"\r{' ' * 40}\r", end="")
            print(f"{C_CYAN}ИИ:{C_RESET} {answer}")
            messages.append({"role": "assistant", "content": answer})
        except Exception as e:
            print(f"\r{' ' * 40}\r", end="")
            print(f"{C_RED}Ошибка:{C_RESET} {e}")
            if messages and messages[-1]["role"] == "user":
                messages.pop()


def main():
    global DEBUG

    parser = argparse.ArgumentParser(description="Тест AI-чата")
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--single", "-s", default=None)
    parser.add_argument("--debug", "-d", action="store_true")
    args = parser.parse_args()

    DEBUG = args.debug

    config_path = find_config(args.config)
    print(f"{C_DIM}Конфиг: {config_path}{C_RESET}")

    cfg = load_config(config_path)
    asyncio.run(interactive_loop(cfg, args.single))


if __name__ == "__main__":
    main()