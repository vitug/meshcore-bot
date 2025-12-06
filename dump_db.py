#!/usr/bin/env python3
"""
MeshCore Bot — Дампер базы данных
Делает полный читаемый дамп всех таблиц SQLite БД в папку dumps/
Форматы: JSON (по таблицам) + один общий SQL-дамп + summary.txt
"""

import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path

DB_PATH = "meshcore_bot.db"          # ← измените, если путь другой
DUMP_ROOT = Path("dumps")

def ensure_dump_dir(timestamp: str):
    dump_dir = DUMP_ROOT / timestamp
    dump_dir.mkdir(parents=True, exist_ok=True)
    return dump_dir

def dump_all_tables_to_json(conn, dump_dir: Path):
    """Дамп каждой таблицы в отдельный JSON-файл"""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()

    summary = {
        "dump_time": datetime.now().isoformat(),
        "database": DB_PATH,
        "tables": []
    }

    for (table_name,) in tables:
        if table_name.startswith("sqlite_"):
            continue

        cursor.execute(f"SELECT COUNT(*) FROM `{table_name}`")
        row_count = cursor.fetchone()[0]

        cursor.execute(f"SELECT * FROM `{table_name}`")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

        data = []
        for row in rows:
            row_dict = {}
            for i, value in enumerate(row):
                # Красиво преобразуем сложные типы
                if isinstance(value, (bytes, bytearray)):
                    row_dict[columns[i]] = value.hex()
                elif value is None:
                    row_dict[columns[i]] = None
                else:
                    row_dict[columns[i]] = value
            data.append(row_dict)

        # Сохраняем как JSON
        json_path = dump_dir / f"{table_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        # Добавляем в общий отчёт
        summary["tables"].append({
            "table": table_name,
            "rows": row_count,
            "json_file": json_path.name
        })

        print(f"✔ Таблица `{table_name}` → {row_count} записей → {json_path.name}")

    # Сохраняем общий отчёт
    with open(dump_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(dump_dir / "summary.txt", "w", encoding="utf-8") as f:
        f.write(f"Дамп базы данных MeshCore Bot\n")
        f.write(f"Время: {summary['dump_time']}\n")
        f.write(f"База: {DB_PATH}\n")
        f.write(f"Всего таблиц: {len(summary['tables'])}\n\n")
        for t in summary["tables"]:
            f.write(f"{t['table']}: {t['rows']} записей\n")
    
    print(f"\nГотово! Дамп сохранён в папку:\n   {dump_dir.resolve()}")

def dump_full_sql(conn, dump_dir: Path):
    """Полный SQL-дамп (можно восстановить через sqlite3)"""
    sql_path = dump_dir / "full_dump.sql"
    with open(sql_path, "w", encoding="utf-8") as f:
        for line in conn.iterdump():
            f.write(f"{line}\n")
    print(f"✔ Полный SQL-дамп → {sql_path.name}")

def main():
    if not Path(DB_PATH).exists():
        print(f"Ошибка: файл базы данных не найден: {DB_PATH}")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_dir = ensure_dump_dir(timestamp)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"Начинаем дамп базы данных {DB_PATH}\n")

    dump_all_tables_to_json(conn, dump_dir)
    dump_full_sql(conn, dump_dir)

    conn.close()

    print(f"\nВсё успешно завершено!")

if __name__ == "__main__":
    main()