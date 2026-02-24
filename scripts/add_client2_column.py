"""One-off script to add `client2_id` column to the `case` table for SQLite.

Usage:
    python scripts/add_client2_column.py

Back up your DB file before running.
"""
from app import create_app
from models import db

def ensure_column():
    app = create_app()
    with app.app_context():
        conn = db.engine.connect()
        try:
            res = conn.execute("PRAGMA table_info('case')").fetchall()
            cols = [r[1] for r in res]
            if 'client2_id' in cols:
                print('client2_id already exists')
                return
            print('Adding client2_id column to case table...')
            conn.execute('ALTER TABLE "case" ADD COLUMN client2_id INTEGER')
            print('Done: client2_id added')
        finally:
            conn.close()

if __name__ == '__main__':
    ensure_column()
