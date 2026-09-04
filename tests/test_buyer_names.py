import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.db_manager import DBManager
from app.services.buyer_names import buyer_names, remember_buyer_names
from utils.seller_order_sync import _save_order


class BuyerNamesTests(unittest.TestCase):
    def setUp(self):
        self.db = DBManager.__new__(DBManager)
        self.db.lock = threading.RLock()
        self.db.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.db.conn.close)
        self.db.conn.executescript('''
            CREATE TABLE cookies(id TEXT, user_id INTEGER);
            INSERT INTO cookies VALUES('shop-a',1),('shop-b',1),('shop-c',2);
            CREATE TABLE user_settings(user_id INTEGER,key TEXT,value TEXT,description TEXT,updated_at TEXT,
                UNIQUE(user_id,key));
        ''')

    def test_scoped_persistent_and_missing_fallback(self):
        remember_buyer_names(self.db, 'shop-a', [('123', '昵称甲')])
        remember_buyer_names(self.db, 'shop-b', [('123', '昵称乙')])
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'123': '昵称甲'})
        self.assertEqual(buyer_names(self.db, 1, 'shop-b'), {'123': '昵称乙'})
        self.assertEqual(buyer_names(self.db, 2, 'shop-a'), {})
        self.assertEqual(buyer_names(self.db, 1, 'shop-c'), {})
        self.assertEqual(buyer_names(self.db, 1, 'missing'), {})
        # Names live in the DB rather than an instance-only runtime map.
        self.assertIn('昵称甲', self.db.conn.execute('SELECT value FROM user_settings').fetchone()[0])

    def test_invalid_names_do_not_overwrite_and_unchanged_does_not_write(self):
        remember_buyer_names(self.db, 'shop-a', [('123', '昵称')])
        with patch.object(self.db, 'set_user_setting', wraps=self.db.set_user_setting) as save:
            for name in ['昵称', '', None, '123', '未知用户', '未知买家']:
                remember_buyer_names(self.db, 'shop-a', [('123', name)])
            save.assert_not_called()
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'123': '昵称'})

    def test_cap_and_corrupt_cache_and_failure_are_safe(self):
        self.db.set_user_setting(1, 'buyer_names:shop-a', 'bad-json')
        with patch('app.services.buyer_names.LIMIT', 2):
            remember_buyer_names(self.db, 'shop-a', [('a', '甲'), ('b', '乙'), ('c', '丙')])
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'b': '乙', 'c': '丙'})
        with patch.object(self.db, 'get_user_setting', side_effect=RuntimeError):
            remember_buyer_names(self.db, 'shop-a', [('d', '丁')])
            self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {})

    def test_seller_sync_uses_nickname_not_recipient_and_failed_save_skips(self):
        record = dict(order_id='order', buyer_id='123', buyer_nick='平台昵称', receiver_name='隐私收货人')
        with patch.object(self.db, 'insert_or_update_order', return_value=True):
            self.assertTrue(_save_order(self.db, 'shop-a', record))
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'123': '平台昵称'})
        with patch.object(self.db, 'insert_or_update_order', return_value=False):
            self.assertFalse(_save_order(self.db, 'shop-a', {**record, 'buyer_nick': '不能覆盖'}))
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'123': '平台昵称'})


if __name__ == '__main__':
    unittest.main()
