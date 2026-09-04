"""Use the real settings table definitions without importing the live DB singleton."""
import ast
from pathlib import Path


def initialize_account_control(db):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'app/db_manager.py').read_text())
    for name in ('ai_reply_settings', 'user_settings'):
        sql = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and f'CREATE TABLE IF NOT EXISTS {name} (' in node.value)
        db.conn.execute(sql)
    db.conn.execute('INSERT INTO ai_reply_settings(cookie_id,ai_enabled) SELECT id,1 FROM cookies')
    db.conn.commit()
