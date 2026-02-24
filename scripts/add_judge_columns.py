#!/usr/bin/env python3
"""
Add missing judge-related columns to the SQLite database used by the app.

Run from the project root:

    python scripts/add_judge_columns.py

This script will look for `legaltech.db` by default (project root). If you
set `SQLALCHEMY_DATABASE_URI` in the environment to a `sqlite:///...` URI,
the script will use that path instead.

Note: SQLite does not support adding UNIQUE constraints via ALTER TABLE.
If you require `judge_id_number` uniqueness at the database level, create a
proper migration (Alembic) or rebuild the table. This script only adds the
columns as nullable TEXT fields so the app can start.
"""
import os
import sys
import sqlite3
import argparse


def get_db_path(cli_path=None):
    # 1) CLI override
    if cli_path:
        return cli_path

    # 2) Prefer environment config if provided
    uri = os.environ.get('SQLALCHEMY_DATABASE_URI') or os.environ.get('DATABASE_URL')
    if uri and uri.startswith('sqlite:///'):
        return uri.replace('sqlite:///', '')

    # 3) Common project locations: instance/legaltech.db then project root legaltech.db
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, '..'))
    candidate = os.path.join(project_root, 'instance', 'legaltech.db')
    if os.path.exists(candidate):
        return candidate
    return os.path.join(project_root, 'legaltech.db')


def main():
    parser = argparse.ArgumentParser(description='Add judge columns to SQLite DB')
    parser.add_argument('--db', dest='db_path', help='Path to sqlite DB file (overrides env)')
    args = parser.parse_args()

    db_path = get_db_path(args.db_path)
    if not os.path.exists(db_path):
        print(f"ERROR: SQLite DB not found at {db_path}")
        print("If your app uses a different DB path, set SQLALCHEMY_DATABASE_URI to a sqlite:///... value and retry.")
        sys.exit(1)

    print(f"Using DB: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info('user')")
    existing = {row[1] for row in cur.fetchall()}  # column names

    to_add = []
    if 'court_name' not in existing:
        to_add.append(("court_name", "TEXT"))
    if 'judge_id_number' not in existing:
        to_add.append(("judge_id_number", "TEXT"))
    if 'verification_document' not in existing:
        to_add.append(("verification_document", "TEXT"))

    if not to_add:
        print("No missing columns detected. Database appears up-to-date.")
        conn.close()
        return

    for col, ctype in to_add:
        sql = f"ALTER TABLE user ADD COLUMN {col} {ctype};"
        print("Executing:", sql)
        cur.execute(sql)

    conn.commit()
    print("Added columns:", ', '.join(c for c, _ in to_add))
    print("Note: UNIQUE constraints are not added. Use Alembic or rebuild table to add constraints.")
    conn.close()


if __name__ == '__main__':
    main()
