#!/usr/bin/env python3
"""
Key command for the MeshCore Bot
Возвращает публичный ключ и имя собственного узла
"""

from .base_command import BaseCommand
from ..models import MeshMessage


class KeyCommand(BaseCommand):
    """Отвечает текущим публичным ключом и именем бота"""
    
    name = "key"
    keywords = ['key', 'ключ', 'pubkey', 'pk', 'пк']
    description = "Показывает публичный ключ и имя этого бота/узла"
    category = "basic"
    cooldown_seconds = 15

    def get_help_text(self) -> str:
        return self.translate('commands.key.help', fallback="Показать публичный ключ и имя этого бота")

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
            response = "❌ Нет подключения к устройству MeshCore"
            return await self.send_response(message, response)

        try:
            # Триггерим обновление self_info
            result = await self.bot.meshcore.commands.send_appstart()
            
            self.logger.debug(f"send_appstart result: {result}")

            if result and result.type == "ERROR":
                response = f"❌ Ошибка send_appstart: {result.payload}"
            else:
                self_info = getattr(self.bot.meshcore, 'self_info', None)
                
                if not self_info or 'public_key' not in self_info:
                    response = "⚠️ Информация об узле ещё не получена (подождите или переподключитесь)"
                else:
                    pubkey = self_info['public_key']
                    name = self_info.get('name', 'Без имени')
                    response = f"🔑 Ключ: {pubkey}\nИмя узла: {name}"

        except Exception as e:
            self.logger.error(f"Исключение при получении ключа: {e}", exc_info=True)
            response = "❌ Ошибка при запросе публичного ключа"

        return await self.send_response(message, response)