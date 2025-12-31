#!/usr/bin/env python3
"""
repeater_purger.py — версия 6: тихое чтение после удаления
"""

import asyncio
import logging
import argparse
import sys
import io
from contextlib import contextmanager
from typing import Optional, Dict

import meshcore
from meshcore import EventType
from meshcore_cli.meshcore_cli import next_cmd

# =============================================================================
# Логирование
# =============================================================================
logger = logging.getLogger("RepeaterPurger")
logger.setLevel(logging.INFO)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(console)

file = logging.FileHandler("repeater_purge.log", encoding="utf-8")
file.setLevel(logging.INFO)
file.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file)

# Убираем дублирование логов от meshcore
logging.getLogger("meshcore").setLevel(logging.WARNING)


# =============================================================================
# Подавление stdout
# =============================================================================
@contextmanager
def suppress_stdout():
    """Подавляет вывод в stdout"""
    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = original


# =============================================================================
# Загрузка контактов (тихая)
# =============================================================================
async def load_contacts_silent(mc: meshcore.MeshCore) -> Dict[str, dict]:
    """Тихая загрузка контактов (без логов)"""
    try:
        with suppress_stdout():
            await next_cmd(mc, ["contacts"])
    except Exception:
        pass
    
    await asyncio.sleep(0.5)
    
    if hasattr(mc, 'contacts') and mc.contacts:
        return dict(mc.contacts)
    return {}


async def load_contacts_initial(mc: meshcore.MeshCore) -> Dict[str, dict]:
    """Начальная загрузка контактов (с логом)"""
    logger.info("Загрузка контактов...")
    
    contacts = await load_contacts_silent(mc)
    
    if contacts:
        logger.info(f"✓ Загружено {len(contacts)} контактов")
    else:
        logger.warning("Контакты не загружены")
    
    return contacts


# =============================================================================
# Определение репитера
# =============================================================================
def is_repeater_device(contact_data: Dict) -> bool:
    """Определение, является ли контакт репитером"""
    try:
        typ = contact_data.get("type")
        if typ in (2, 3):
            return True

        mode = str(contact_data.get("mode", "")).lower()
        if "repeater" in mode or "room" in mode:
            return True

        name = contact_data.get("adv_name", contact_data.get("name", "")).lower()
        keywords = ["repeater", "room", "server", "rs", "rpt", "relay", "rep"]
        if any(kw in name for kw in keywords):
            return True

        return False
    except Exception:
        return False


# =============================================================================
# Удаление контакта
# =============================================================================
async def remove_contact(mc: meshcore.MeshCore, contact_key: str, contact_data: Dict) -> bool:
    """Удаление контакта с тихой проверкой"""
    name = contact_data.get("adv_name", contact_data.get("name", "Unknown"))
    public_key = contact_data.get("public_key", contact_key)
    
    # Удаление через CLI
    try:
        with suppress_stdout():
            await next_cmd(mc, ["remove_contact", public_key])
    except Exception as e:
        logger.warning(f"  ✗ Ошибка CLI: {e}")
    
    # Удаление через API
    try:
        if hasattr(mc.commands, 'remove_contact'):
            with suppress_stdout():
                await mc.commands.remove_contact(public_key)
    except Exception:
        pass
    
    # Тихая проверка (без логов о содержимом)
    await asyncio.sleep(0.5)
    await load_contacts_silent(mc)
    
    # Проверяем, удалён ли контакт
    still_exists = any(
        d.get("public_key") == public_key or k == contact_key
        for k, d in mc.contacts.items()
    )
    
    if still_exists:
        logger.warning(f"  ✗ Не удалён")
        return False
    else:
        logger.info(f"  ✓ Удалён")
        return True


# =============================================================================
# Основная логика
# =============================================================================
async def main():
    parser = argparse.ArgumentParser(description="Удаление репитеров из MeshCore")
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Удалить все репитеры")
    group.add_argument("--name", type=str, help="Удалить по частичному совпадению имени")
    group.add_argument("--force-name", type=str, help="Удалить по ТОЧНОМУ имени")
    group.add_argument("--list", action="store_true", help="Только показать контакты")
    
    parser.add_argument("--dry-run", action="store_true", help="Только показать, не удалять")
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробный вывод")
    
    conn = parser.add_argument_group("Подключение")
    conn_type = conn.add_mutually_exclusive_group(required=True)
    conn_type.add_argument("--serial", type=str, help="COM-порт")
    conn_type.add_argument("--ble", type=str, help="BLE устройство")
    conn_type.add_argument("--tcp", type=str, metavar="HOST:PORT")
    
    args = parser.parse_args()
    
    # Подключение
    mc: Optional[meshcore.MeshCore] = None
    try:
        with suppress_stdout():
            if args.serial:
                logger.info(f"Подключение к {args.serial}...")
                mc = await meshcore.MeshCore.create_serial(args.serial)
            elif args.ble:
                logger.info(f"Подключение к BLE {args.ble}...")
                mc = await meshcore.MeshCore.create_ble(args.ble)
            elif args.tcp:
                host, port = args.tcp.split(":")
                logger.info(f"Подключение к TCP {host}:{port}...")
                mc = await meshcore.MeshCore.create_tcp(host, int(port))
        
        if not mc or not mc.is_connected:
            logger.error("Не удалось подключиться")
            return
        
        logger.info("✓ Подключено")
        mc.auto_update_contacts = True
        
    except Exception as e:
        logger.error(f"Ошибка подключения: {e}")
        return
    
    # Начальная загрузка контактов
    contacts = await load_contacts_initial(mc)
    
    if not contacts:
        logger.error("Контакты не загружены!")
        return
    
    # Подсчёт статистики
    repeaters = {k: v for k, v in contacts.items() if is_repeater_device(v)}
    clients = {k: v for k, v in contacts.items() if not is_repeater_device(v)}
    
    logger.info(f"Репитеров: {len(repeaters)} | Клиентов: {len(clients)}")
    
    # Вывод списка контактов (только если --list или --verbose)
    if args.list or args.verbose:
        print("\n" + "="*60)
        print("РЕПИТЕРЫ:")
        print("="*60)
        for pk, data in sorted(repeaters.items(), key=lambda x: x[1].get('adv_name', '')):
            name = data.get('adv_name', data.get('name', 'Unknown'))
            print(f"  {name}")
        
        print("\n" + "="*60)
        print("КЛИЕНТЫ:")
        print("="*60)
        for pk, data in sorted(clients.items(), key=lambda x: x[1].get('adv_name', '')):
            name = data.get('adv_name', data.get('name', 'Unknown'))
            print(f"  {name}")
        
        if args.list:
            return
    
    # Поиск целей для удаления
    targets = []
    for pk, data in contacts.items():
        name = data.get('adv_name', data.get('name', ''))
        
        if args.force_name:
            if name == args.force_name:
                targets.append((pk, data))
        elif args.name:
            if args.name.lower() in name.lower():
                targets.append((pk, data))
        elif args.all:
            if is_repeater_device(data):
                targets.append((pk, data))
    
    if not targets:
        logger.info("Нет контактов для удаления")
        return
    
    logger.info(f"Найдено целей: {len(targets)}")
    
    if args.dry_run:
        logger.info("=== DRY-RUN ===")
        for pk, data in targets:
            name = data.get('adv_name', data.get('name', 'Unknown'))
            print(f"  → {name}")
        return
    
    # Удаление
    logger.info("Удаление...")
    removed = 0
    failed = 0
    
    for i, (pk, data) in enumerate(targets, 1):
        name = data.get('adv_name', data.get('name', 'Unknown'))
        logger.info(f"[{i}/{len(targets)}] {name}")
        
        if await remove_contact(mc, pk, data):
            removed += 1
        else:
            failed += 1
    
    # Итог
    logger.info(f"\nРезультат: удалено {removed}, ошибок {failed}")


if __name__ == "__main__":
    asyncio.run(main())