import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, Mock

from app.db_manager import DBManager
from app.services.buyer_names import buyer_names, remember_buyer_names, enrich_conversation_names
from utils.seller_order_sync import _save_order


class BuyerNamesTests(unittest.TestCase):
    def setUp(self):
        self.db = DBManager.__new__(DBManager)
        self.db.lock = threading.RLock()
        self.db.conn = sqlite3.connect(':memory:', check_same_thread=False)
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

    def test_cached_nick_is_shared_across_chats_not_accounts(self):
        remember_buyer_names(self.db, 'shop-a', [('123', 'AI研究所')])
        remember_buyer_names(self.db, 'shop-b', [('123', '另一店昵称')])
        conversations = [{'cid': 'chat-a', 'otherUserId': '123', 'otherUserName': ''},
                         {'cid': 'chat-b', 'otherUserId': '123', 'otherUserName': '快给ta一个评价吧～'},
                         {'cid': 'chat-c', 'otherUserId': '123', 'otherUserName': '旧会话昵称'}]
        original_ids = [(c['cid'], c['otherUserId']) for c in conversations]
        enrich_conversation_names(self.db, 1, 'shop-a', conversations)
        self.assertEqual([c['otherUserName'] for c in conversations], ['AI研究所'] * 3)
        self.assertEqual([(c['cid'], c['otherUserId']) for c in conversations], original_ids)
        self.assertEqual(buyer_names(self.db, 1, 'shop-b'), {'123': '另一店昵称'})

    def test_old_bad_cache_is_ignored_without_database_repair(self):
        value = '{"123":"快给ta一个评价吧～","456":"真实昵称"}'
        self.db.set_user_setting(1, 'buyer_names:shop-a', value)
        self.assertEqual(buyer_names(self.db, 1, 'shop-a'), {'456': '真实昵称'})
        self.assertEqual(self.db.get_user_setting(1, 'buyer_names:shop-a')['value'], value)
        conversations = [{'otherUserId': '123', 'otherUserName': '恢复昵称'}, {'otherUserId': '456', 'otherUserName': ''}]
        enrich_conversation_names(self.db, 1, 'shop-a', conversations)
        self.assertEqual([c['otherUserName'] for c in conversations], ['恢复昵称', '真实昵称'])

    def test_historical_name_cannot_overwrite_but_live_explicit_name_can(self):
        remember_buyer_names(self.db, 'shop-a', [('123', '现在昵称')])
        remember_buyer_names(self.db, 'shop-a', [('123', '历史昵称')], overwrite=False)
        self.assertEqual(buyer_names(self.db, 1, 'shop-a')['123'], '现在昵称')
        remember_buyer_names(self.db, 'shop-a', [('123', '新昵称')])
        self.assertEqual(buyer_names(self.db, 1, 'shop-a')['123'], '新昵称')

    def test_notification_name_only_resolves_after_login_and_account_checks(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.desktop_notifications import DesktopNotifications
        from app.routers.desktop_notifications import create_desktop_notifications_router
        remember_buyer_names(self.db, 'shop-a', [('123', '通知买家昵称')])
        hub = DesktopNotifications(clock=lambda: 1000)
        hub.configure('auth', 1, True)
        hub.publish(user_id=1, account_id='shop-a', sender_id='123', own_id='seller', chat_id='cid',
                    timestamp_ms=1000000, message_id='m', content='私密消息正文')
        ticket = hub.poll(0, lambda *_: True)['target']
        app = FastAPI()
        app.include_router(create_desktop_notifications_router(hub, 'native',
            lambda cred: {'user_id': 1} if cred and cred.credentials == 'auth' else None, self.db))
        with TestClient(app) as client, patch.object(self.db, 'get_all_cookies', return_value={'shop-a': 'unused'}):
            native = {'X-Xianyu-Desktop-Token': 'native'}
            auth = {'Authorization': 'Bearer auth'}
            self.assertNotIn('通知买家昵称', client.get('/desktop/notifications/poll', headers=native).text)
            client.post('/desktop/notifications/activate', headers=native, json={'target': ticket})
            self.assertEqual(client.post('/desktop/notifications/activation').status_code, 401)
            target = client.post('/desktop/notifications/activation', headers=auth).json()['navigation']
            self.assertEqual((target['account_id'], target['chat_id'], target['buyer_id'], target['buyer_name']),
                             ('shop-a', 'cid', '123', '通知买家昵称'))
            client.post('/desktop/notifications/activate', headers=native, json={'target': ticket})
            with patch.object(self.db, 'get_all_cookies', return_value={}):
                self.assertEqual(set(client.post('/desktop/notifications/activation', headers=auth).json()['navigation']), {'id'})


class ChatNameRouteTests(unittest.IsolatedAsyncioTestCase):
    setUp = BuyerNamesTests.setUp

    def route(self, name, body):
        from fastapi import HTTPException
        from test_delivery_receipts import load_route
        async def dedup(key, operation):
            return await operation()
        self.request = AsyncMock(return_value=body)
        self.guard = Mock()
        return load_route(name, dict(db_manager=self.db, _get_owned_chat_account=self.guard,
            account_request_dedup=SimpleNamespace(run=dedup), _run_on_account_loop=self.request,
            cookie_manager=SimpleNamespace(manager=SimpleNamespace(instances={'shop-a': SimpleNamespace(myid='seller')})),
            HTTPException=HTTPException))

    async def test_conversation_endpoint_uses_shared_cached_name_for_system_and_self_last(self):
        remember_buyer_names(self.db, 'shop-a', [('123', 'AI研究所')])
        raw = lambda cid, sender: {'singleChatConversation': {'cid': cid + '@goofish', 'pairFirst': '123@goofish', 'pairSecond': 'seller@goofish'},
            'lastMessage': {'message': {'extension': {'senderUserId': sender, 'reminderTitle': '快给ta一个评价吧～'},
                                      'content': {'custom': {'summary': '快给ta一个评价吧～'}}}}}
        endpoint = self.route('get_chat_conversations', {'userConvs': [raw('one', '123'), raw('two', 'seller')]})
        result = await endpoint('shop-a', None, 30, {'user_id': 1})
        conversations = result['data']['conversations']
        self.assertEqual([(c['cid'], c['otherUserId'], c['otherUserName']) for c in conversations],
                         [('one', '123', 'AI研究所'), ('two', '123', 'AI研究所')])
        self.assertEqual(self.request.await_count, 1)  # No extra platform nickname requests.

    async def test_history_endpoint_supplies_cached_sender_name_without_changing_message(self):
        remember_buyer_names(self.db, 'shop-a', [('123', 'AI研究所')])
        model = {'message': {'messageId': 'm', 'extension': {'senderUserId': '123', 'reminderTitle': '快给ta一个评价吧～'},
                             'content': {'custom': {'summary': '原消息预览'}}}}
        endpoint = self.route('get_chat_messages', {'data': {'userMessageModels': [model]}})
        result = await endpoint('shop-a', 'cid', None, 50, {'user_id': 1})
        message = result['data']['messages'][0]
        self.assertEqual((message['senderId'], message['senderName'], message['text']), ('123', 'AI研究所', '原消息预览'))
        self.assertEqual(self.request.await_count, 1)


if __name__ == '__main__':
    unittest.main()
