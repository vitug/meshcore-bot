#!/usr/bin/env python3
"""
Key command for the MeshCore Bot
Возвращает публичный ключ и имя узла (своего или по поиску имени)
При поиске по имени:
- 1 узел → имя + ключ
- >1 узла → только список имён (без ключей)
"""
import re
import asyncio
from typing import List, Optional
from .base_command import BaseCommand
from ..models import MeshMessage

class KeyCommand(BaseCommand):
    """Отвечает текущим публичным ключом и именем бота или другого узла"""

    name = "key"
    keywords = ['key', 'ключ', 'pubkey', 'pk', 'пк', 'k', 'к']
    description = "Показывает публичный ключ бота или другого узла. key <имя> — поиск по подстроке"
    category = "basic"
    cooldown_seconds = 15
    requires_dm = False

    def get_help_text(self) -> str:
        return "key → own pubkey\nkey <query> → search nodes by name substring"

    def matches_keyword(self, message: MeshMessage) -> bool:
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        lower = content.lower()
        for kw in self.keywords:
            kw_low = kw.lower()
            if lower == kw_low or (lower.startswith(kw_low + ' ') and len(content) > len(kw_low) + 1):
                return True
        return False

    async def execute(self, message: MeshMessage) -> bool:
        if not self.bot.meshcore or not self.bot.connected:
            await self.send_response(message, "❌ Нет подключения к устройству MeshCore")
            return True

        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()

        parts = re.split(r'\s+', content, maxsplit=1)
        keyword = parts[0].lower()
        search_query = parts[1].strip() if len(parts) > 1 else None

        if not search_query:
            # Свой ключ
            response = await self._get_self_pubkey()
        else:
            # Поиск по имени
            response = await self._search_node_by_name(search_query)

        await self._send_response(message, response)
        return True

    async def _get_self_pubkey(self) -> str:
        try:
            await self.bot.meshcore.commands.send_appstart()

            self_info = getattr(self.bot.meshcore, 'self_info', None)
            if not self_info or 'public_key' not in self_info:
                return "⚠️ Информация об узле ещё не получена"

            pubkey = self_info['public_key']
            name = self_info.get('name', 'Без имени')
            return f"🔑 {pubkey}\nИмя: {name}"
        except Exception as e:
            self.logger.error(f"Ошибка получения своего ключа: {e}", exc_info=True)
            return "❌ Ошибка при запросе своего публичного ключа"

    async def _search_node_by_name(self, search_query: str) -> str:
        try:
            # Запрашиваем все подходящие записи
            query = '''
                SELECT name, public_key, role, 
                       last_heard,
                       last_advert_timestamp
                FROM complete_contact_tracking
                WHERE name LIKE ?
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 15
            '''
            pattern = f"%{search_query}%"
            rows = self.bot.db_manager.execute_query(query, (pattern,))

            if not rows:
                return f"По запросу «{search_query}» ничего не найдено"

            # ───────────────────────────────────────────────
            # Группируем по точному имени (case-sensitive)
            from collections import defaultdict
            by_exact_name = defaultdict(list)

            for row in rows:
                exact_name = row['name']
                by_exact_name[exact_name].append(row)

            # ───────────────────────────────────────────────
            # Случай 1: найдено ровно одно уникальное имя
            if len(by_exact_name) == 1:
                exact_name, candidates = next(iter(by_exact_name.items()))

                # Если по этому имени несколько записей → берём самую свежую
                if len(candidates) > 1:
                    # Сортировка уже по убыванию свежести, берём первую
                    best = candidates[0]
                else:
                    best = candidates[0]

                name = best['name']
                if len(name) > 30:
                    name = name[:27] + "..."

                pubkey = best['public_key']
                role = best.get('role', '?')

                return f"{name} ({role})\n🔑 {pubkey}"

            # ───────────────────────────────────────────────
            # Случай 2: несколько разных имён → показываем только список имён
            else:
                # Собираем самые свежие записи для каждого уникального имени
                latest_per_name = []
                for exact_name, candidates in by_exact_name.items():
                    # Самая свежая запись для этого имени
                    best = candidates[0]  # уже отсортированы по свежести
                    last_time = best.get('last_advert_timestamp') or best.get('last_heard') or ''
                    latest_per_name.append((exact_name, last_time))

                # Сортируем по алфавиту имени для предсказуемости
                latest_per_name.sort(key=lambda x: x[0].lower())

                names = []
                for name, _ in latest_per_name:
                    display_name = name
                    if len(display_name) > 28:
                        display_name = display_name[:25] + "..."
                    names.append(display_name)

                header = f"Found {len(names)} names matching “{search_query}”"
                if len(names) >= 15:
                    header += " (top 15)"

                lines = [header]
                for i, name in enumerate(names, 1):
                    lines.append(f"{i}. {name}")

                return "\n".join(lines)

        except Exception as e:
            self.logger.error(f"Ошибка поиска узлов по имени: {e}", exc_info=True)
            return f"Ошибка поиска: {str(e)[:80]}"

    async def _send_response(self, message: MeshMessage, text: str):
        """Отправка с разбивкой на части, если сообщение слишком длинное"""
        self.last_response = text  # для web-viewer

        MAX_LEN = 180  # чуть больше, чем обычно для LoRa/MeshCore

        if len(text) <= MAX_LEN:
            await self.send_response(message, text)
            return

        lines = text.split('\n')
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > MAX_LEN:
                if chunk:
                    await self.send_response(message, chunk.strip())
                    await asyncio.sleep(2.5)
                chunk = line
            else:
                if chunk:
                    chunk += "\n" + line
                else:
                    chunk = line

        if chunk:
            await self.send_response(message, chunk.strip())