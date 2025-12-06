#!/usr/bin/env python3
"""
MeshCore — Карта занятости первых байтов public_key
ТОЛЬКО для репитеров (role = 'repeater')
Работает с вашей реальной таблицей complete_contact_tracking
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import binascii

DB_PATH = "meshcore_bot.db"
OUTPUT_DIR = Path("repeaters_map")
OUTPUT_DIR.mkdir(exist_ok=True)

# === Цвета для SVG (тёмная тема) ===
COLOR_FREE     = "#0d1117"
COLOR_1        = "#f85149"   # 1 репитер
COLOR_2_4      = "#f0883e"   # 2–4
COLOR_5_9      = "#f6d05e"   # 5–9
COLOR_10_PLUS  = "#7ee787"   # 10+ — популярный префикс

def byte_to_color(count: int) -> str:
    if count == 0: return COLOR_FREE
    if count == 1: return COLOR_1
    if count <= 4: return COLOR_2_4
    if count <= 9: return COLOR_5_9
    return COLOR_10_PLUS

def hex_to_bytes(hex_str: str) -> bytes:
    """Конвертирует hex-строку в bytes, убирая пробелы и 0x"""
    clean = hex_str.strip().replace(" ", "").replace("0x", "")
    if len(clean) % 2 != 0:
        clean = "0" + clean
    return binascii.unhexlify(clean)

def get_repeaters_only(conn):
    """Извлекает только репитеры из complete_contact_tracking"""
    cursor = conn.cursor()

    # Проверяем таблицу
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='complete_contact_tracking'")
    if not cursor.fetchone():
        print("Ошибка: таблица complete_contact_tracking не найдена!")
        return {}

    query = """
    SELECT id, public_key, name, role, last_heard, latitude, longitude, city
    FROM complete_contact_tracking 
    WHERE role = 'repeater'
      AND public_key IS NOT NULL 
      AND length(public_key) >= 64   -- минимум 32 байта в hex = 64 символа
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    repeaters = {}
    for row in rows:
        db_id, pubkey_hex, name, role, last_heard, lat, lon, city = row

        try:
            pubkey_bytes = hex_to_bytes(pubkey_hex)
            if len(pubkey_bytes) < 32:
                continue
            first_byte = pubkey_bytes[0]
        except:
            print(f"Ошибка парсинга public_key у записи id={db_id}")
            continue

        name = (name or "NoName").strip().replace("\ud83d", "").replace("\ud83c", "")

        repeaters[db_id] = {
            "id": db_id,
            "name": name,
            "public_key_hex": pubkey_hex,
            "first_byte": first_byte,
            "first_byte_hex": f"0x{first_byte:02X}",
            "last_heard": last_heard,
            "location": f"{lat},{lon}" if lat and lon else None,
            "city": city
        }

    return repeaters

def generate_svg(usage: dict, total: int):
    cell = 38
    grid = 16
    w = cell * grid + 160
    h = cell * grid + 180

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">',
        '  <rect width="100%" height="100%" fill="#0d1117"/>',
        f'  <text x="28" y="44" fill="#58a6ff" font-size="32" font-weight="600">MeshCore Repeaters Map</text>',
        f'  <text x="28" y="82" fill="#8b949e" font-size="18">Только role="repeater" • Найдено: {total} • {datetime.now():%Y-%m-%d %H:%M}</text>',
    ]

    # Легенда
    ly = h - 70
    legends = [
        (28,  "0", COLOR_FREE),
        (160, "1", COLOR_1),
        (280, "2–4", COLOR_2_4),
        (420, "5–9", COLOR_5_9),
        (560, "10+", COLOR_10_PLUS),
    ]
    for x, label, color in legends:
        lines.append(f'  <rect x="{x}" y="{ly}" width="40" height="40" fill="{color}" rx="10"/>')
        lines.append(f'  <text x="{x+50}" y="{ly+30}" fill="#f0f6fc" font-size="18">{label}</text>')

    # Сетка 16×16
    for row in range(16):
        for col in range(16):
            byte_val = row * 16 + col
            count = len(usage.get(byte_val, []))
            color = byte_to_color(count)

            x = 28 + col * cell
            y = 110 + row * cell

            lines.append(f'  <rect x="{x}" y="{y}" width="{cell-5}" height="{cell-5}" fill="{color}" rx="10" stroke="#30363d"/>')
            lines.append(f'  <text x="{x+8}" y="{y+26}" fill="#f0f6fc" font-size="15" font-weight="bold">{byte_val:02X}</text>')
            if count > 0:
                lines.append(f'  <text x="{x+8}" y="{y+46}" fill="#f0f6fc" font-size="12">{count}</text>')

    lines.append('</svg>')
    return "\n".join(lines)

def main():
    if not Path(DB_PATH).exists():
        print(f"База не найдена: {DB_PATH}")
        return

    print(f"Анализ репитеров из {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    repeaters = get_repeaters_only(conn)
    conn.close()

    total = len(repeaters)
    print(f"Найдено репитеров: {total}")

    if total == 0:
        print("Репитеры не найдены. Проверьте, есть ли в сети устройства с role='repeater'.")
        return

    # Группировка по первому байту
    usage = defaultdict(list)
    for info in repeaters.values():
        usage[info["first_byte"]].append({
            "name": info["name"],
            "id": info["id"],
            "city": info["city"],
            "last_heard": info["last_heard"]
        })

    occupied = len(usage)
    free = 256 - occupied

    # === Текстовый отчёт ===
    txt_path = OUTPUT_DIR / "repeaters_only.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"MeshCore — Активные репитеры (role = 'repeater')\n")
        f.write(f"Всего найдено: {total} | Занято первых байтов: {occupied} из 256 | Свободно: {free}\n")
        f.write(f"Дата сканирования: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 100 + "\n\n")

        for b in sorted(usage.keys()):
            nodes = sorted(usage[b], key=lambda x: x["name"])
            f.write(f"0x{b:02X} → {len(nodes)} репитер(ов)\n")
            for n in nodes:
                city = f" ({n['city']})" if n['city'] else ""
                f.write(f"  • {n['name']}{city} [id={n['id']}]\n")
            f.write("\n")

        f.write(f"СВОБОДНЫХ первых байтов: {free} — можно использовать для новых репитеров!\n")

    print(f"Текстовый отчёт → {txt_path.name}")

    # === JSON ===
    json_path = OUTPUT_DIR / "repeaters_summary.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "total_repeaters": total,
            "occupied_first_bytes": occupied,
            "free_first_bytes": free,
            "by_first_byte": {f"0x{b:02X}": len(usage.get(b, [])) for b in range(256)},
            "repeaters": repeaters
        }, f, ensure_ascii=False, indent=2)

    # === SVG ===
    svg_path = OUTPUT_DIR / "repeaters_map.svg"
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(generate_svg(usage, total))

    print(f"SVG карта → {svg_path.name}")
    print(f"JSON данные → {json_path.name}")
    print(f"\nГотово! Все файлы в папке:\n   {OUTPUT_DIR.resolve()}")

if __name__ == "__main__":
    main()