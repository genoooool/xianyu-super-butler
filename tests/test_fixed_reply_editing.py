"""Offline QA editing: stable identity, multi-product isolation, conflicts and deletion."""
import sqlite3
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import KnowledgeService, initialize_schema
from tests.test_fixed_replies import Fixture, png


class EditingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.db.conn.execute("INSERT INTO item_info VALUES('a','three'),('b','b-only')")
        self.db.conn.commit()

    def edit(self, row, **changes):
        return self.knowledge.save(1, {**row, **changes}, entry_id=row['id'], expected_revision=row['revision'])

    def multi(self, **changes):
        return self.knowledge.save(1, dict(scope='item', cookie_id='a', item_ids=['one', 'two'],
            topic='问候', keywords='你好', content='  固定问候\n原文  ', entry_type='qa', **changes), create_only=True)

    def test_one_rule_for_two_products_never_other_store_or_unselected_product(self):
        row = self.multi()
        model = Mock(side_effect=AssertionError('must not call model'))
        for product in ('one', 'two'):
            result = self.fixed.choose('a', product, '你好！', model)
            self.assertEqual(result.rule['id'], row['id'])
            self.assertEqual(result.rule['content'], row['content'])
        self.assertEqual(self.fixed.choose('a', 'three', '你好').status, 'no_match')
        self.assertEqual(self.fixed.choose('b', 'one', '你好').status, 'no_match')
        self.assertEqual(self.fixed.choose('other', 'one', '你好').status, 'no_match')
        self.assertEqual(len(self.knowledge.list_entries(1)), 1)
        model.assert_not_called()

    def test_edit_intent_and_scope_keeps_id_and_does_not_leave_old_rule(self):
        row = self.multi()
        changed = self.edit(row, scope='account', item_ids=[], item_id='', topic='新问候', content='新原文')
        self.assertEqual(changed['id'], row['id'])
        self.assertEqual(changed['revision'], row['revision'] + 1)
        self.assertEqual(len(self.knowledge.list_entries(1)), 1)
        for product in ('one', 'two', 'three'):
            self.assertEqual(self.fixed.choose('a', product, '你好').rule['content'], '新原文')
        self.assertEqual(self.fixed.choose('b', 'one', '你好').status, 'no_match')
        shared = self.edit(changed, scope='shared', cookie_id='')
        self.assertEqual(self.fixed.choose('b', 'one', '你好').rule['id'], shared['id'])
        self.assertEqual(self.fixed.choose('other', 'one', '你好').status, 'no_match')

    def test_modify_selected_products_and_answer_updates_together(self):
        row = self.multi()
        pending = self.fixed.choose('a', 'two', '你好')
        changed = self.edit(row, item_id='three', item_ids=['two', 'three'], content='更新原文')
        self.assertEqual(changed['id'], row['id'])
        self.assertEqual(self.fixed.choose('a', 'one', '你好').status, 'no_match')
        for product in ('two', 'three'):
            self.assertEqual(self.fixed.choose('a', product, '你好').rule['content'], '更新原文')
        self.assertFalse(self.fixed.revalidate(pending, 'a', 'two'))

    def test_item_override_applies_only_to_selected_products(self):
        store = self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='问候',
            keywords='你好', content='店铺问候', entry_type='qa'))
        row = self.multi()
        self.assertEqual(self.fixed.choose('a', 'two', '你好').rule['id'], row['id'])
        self.assertEqual(self.fixed.choose('a', 'three', '你好').rule['id'], store['id'])
        self.edit(row, enabled=False)
        self.assertEqual(self.fixed.choose('a', 'two', '你好').rule['id'], store['id'])

    def test_cross_store_or_unknown_product_rejects_entire_edit(self):
        row = self.multi()
        for ids in (['one', 'b-only'], ['one', 'missing']):
            with self.assertRaises(ValueError):
                self.edit(row, item_ids=ids)
            self.assertEqual(self.knowledge.list_entries(1), [row])
        with self.assertRaises(PermissionError):
            self.edit(row, cookie_id='other')
        self.assertEqual(self.knowledge.list_entries(1), [row])

    def test_same_topic_overlap_is_conflict_even_when_disabled(self):
        row = self.multi(enabled=False)
        other = self.knowledge.save(1, dict(scope='item', cookie_id='a', item_id='three',
            topic='问候', keywords='你好', content='第三商品问候', entry_type='qa'))
        with self.assertRaises(FileExistsError):
            self.edit(other, item_id='two', item_ids=['two', 'three'])
        with self.assertRaises(FileExistsError):
            self.knowledge.save(1, {**row, 'item_id': 'two', 'item_ids': ['two']}, create_only=True)
        self.assertEqual(len(self.knowledge.list_entries(1)), 2)

    def test_rename_and_move_conflicts_preserve_both_rows(self):
        row = self.multi()
        store = self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='目标主题',
            content='不能覆盖', entry_type='qa'))
        with self.assertRaises(FileExistsError):
            self.edit(row, scope='account', item_id='', item_ids=[], topic='目标主题')
        self.assertEqual({e['id']: e for e in self.knowledge.list_entries(1)}, {row['id']: row, store['id']: store})

    def test_stale_edit_and_delete_rejected_and_deleted_rule_not_recreated(self):
        row = self.multi()
        pending = self.fixed.choose('a', 'one', '你好')
        new = self.edit(row, keywords='您好')
        with self.assertRaises(FileExistsError): self.edit(row, content='旧页面覆盖')
        with self.assertRaises(FileExistsError): self.knowledge.delete_qa(1, row['id'], row['revision'])
        self.knowledge.delete_qa(1, new['id'], new['revision'])
        self.assertEqual(self.knowledge.list_entries(1), [])
        self.assertFalse(self.fixed.revalidate(pending, 'a', 'one'))
        with self.assertRaises(PermissionError): self.edit(new, content='不能复活')

    def test_delete_keeps_reused_images_and_other_rules(self):
        asset = self.assets.save(1, png())
        row = self.multi(image_ids=[asset['id']])
        other = self.rule(image_ids=[asset['id']])
        self.knowledge.delete_qa(1, row['id'], row['revision'])
        self.assertEqual(self.knowledge.list_entries(1), [other])
        self.assertEqual(self.assets.get(1, asset['id'])['data'], png())

    def test_backup_roundtrip_preserves_multiselect_and_old_backup_still_works(self):
        row = self.multi()
        cursor = self.db.conn.execute('SELECT * FROM ai_knowledge_entries')
        backup = dict(columns=[col[0] for col in cursor.description], rows=cursor.fetchall())
        self.knowledge.restore_backup(backup, 1); self.db.conn.commit()
        self.assertEqual(self.knowledge.list_entries(1)[0]['item_ids'], ['one', 'two'])
        self.knowledge.delete_qa(1, row['id'], self.knowledge.list_entries(1)[0]['revision'])
        legacy = dict(columns=['scope', 'cookie_id', 'item_id', 'topic', 'content', 'entry_type'],
            rows=[['item', 'a', 'one', '旧单商品', '旧答案', 'qa']])
        self.knowledge.restore_backup(legacy, 1); self.db.conn.commit()
        self.assertEqual(self.knowledge.list_entries(1)[0]['item_ids'], ['one'])

    def test_invalid_item_lists_do_not_save(self):
        for ids in ([''], [' one'], [1], {'one': True}, ['x'] * 201):
            with self.subTest(ids=str(ids)[:30]), self.assertRaises(ValueError):
                self.knowledge.save(1, dict(scope='item', cookie_id='a', item_ids=ids,
                    topic='问候', content='固定答案', entry_type='qa'))
        with self.assertRaises(ValueError):
            self.knowledge.save(1, dict(scope='account', cookie_id='a', item_ids=['one'], topic='问候', content='答案'))
        self.assertEqual(self.knowledge.list_entries(1), [])

    def test_schema_addition_preserves_old_rows_and_single_item_membership(self):
        # Construct a legacy table in another memory-only database; no real profile imports.
        legacy = sqlite3.connect(':memory:')
        try:
            self.multi()
            cursor = self.db.conn.execute('SELECT * FROM ai_knowledge_entries')
            columns = [col[0] for col in cursor.description if col[0] != 'item_ids']
            values = self.db.conn.execute('SELECT '+','.join(columns)+' FROM ai_knowledge_entries').fetchall()
            legacy.execute('CREATE TABLE ai_knowledge_entries ('+','.join(columns)+')')
            legacy.executemany('INSERT INTO ai_knowledge_entries VALUES ('+','.join('?' for _ in columns)+')', values)
            initialize_schema(legacy.cursor()); initialize_schema(legacy.cursor())
            self.assertEqual(legacy.execute('SELECT '+','.join(columns)+' FROM ai_knowledge_entries').fetchall(), values)
            old = KnowledgeService._rows(legacy.execute('SELECT * FROM ai_knowledge_entries'))[0]
            self.assertEqual(old['item_ids'], [old['item_id']])
        finally:
            legacy.close()

    def test_concurrent_connections_only_one_stale_edit_can_commit(self):
        row = self.multi()
        path = Path(tempfile.mkdtemp(prefix='xianyu-qa-concurrency-'))/'isolated.sqlite3'
        first = sqlite3.connect(path, check_same_thread=False)
        self.db.conn.backup(first)
        second = sqlite3.connect(path, check_same_thread=False)
        barrier = threading.Barrier(2)
        results = []
        def edit(conn, text):
            service = KnowledgeService(SimpleNamespace(conn=conn, lock=threading.RLock()))
            barrier.wait(timeout=5)
            try:
                service.save(1, {**row, 'content': text}, entry_id=row['id'], expected_revision=row['revision'])
                results.append('saved')
            except FileExistsError:
                results.append('conflict')
        workers = [threading.Thread(target=edit, args=(first, '第一份修改')),
                   threading.Thread(target=edit, args=(second, '第二份修改'))]
        try:
            for worker in workers: worker.start()
            for worker in workers: worker.join(timeout=10)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(sorted(results), ['conflict', 'saved'])
            self.assertEqual(first.execute('SELECT revision FROM ai_knowledge_entries').fetchone()[0], row['revision'] + 1)
        finally:
            first.close(); second.close()


class EditingApiTests(Fixture):
    def setUp(self):
        super().setUp()
        def user(authorization: str = Header(default='')):
            if authorization not in {'Bearer one', 'Bearer two'}: raise HTTPException(401)
            return {'user_id': 1 if authorization == 'Bearer one' else 2}
        app = FastAPI(); app.include_router(create_ai_knowledge_router(user, self.db))
        self.client = TestClient(app)
        self.auth = {'Authorization': 'Bearer one'}
        self.body = dict(scope='item', cookie_id='a', item_ids=['one', 'two'], topic='问候', keywords='你好', content='固定答案')

    def test_authenticated_create_update_delete_and_stale_guard(self):
        self.assertEqual(self.client.post('/ai-knowledge/qa', json=self.body).status_code, 401)
        row = self.client.post('/ai-knowledge/qa', json=self.body, headers=self.auth).json()['entry']
        endpoint = '/ai-knowledge/qa/'+str(row['id'])
        updated = self.client.put(endpoint, json={**row, 'topic': '新问候'}, headers=self.auth)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['entry']['id'], row['id'])
        self.assertEqual(self.client.put(endpoint, json=row, headers=self.auth).status_code, 409)
        self.assertEqual(self.client.delete(endpoint, params={'revision': row['revision']}, headers=self.auth).status_code, 409)
        self.assertEqual(self.client.delete(endpoint, params={'revision': updated.json()['entry']['revision']}, headers=self.auth).status_code, 200)
        self.assertEqual(self.client.put(endpoint, json=updated.json()['entry'], headers=self.auth).status_code, 404)

    def test_no_cross_owner_access_and_no_knowledge_conversion(self):
        row = self.client.post('/ai-knowledge/qa', json=self.body, headers=self.auth).json()['entry']
        endpoint = '/ai-knowledge/qa/'+str(row['id'])
        foreign = {'Authorization': 'Bearer two'}
        self.assertEqual(self.client.put(endpoint, json=row, headers=foreign).status_code, 404)
        self.assertEqual(self.client.delete(endpoint, params={'revision': 1}, headers=foreign).status_code, 404)
        self.assertEqual(self.client.put(endpoint, json={**row, 'entry_type': 'knowledge'}, headers=self.auth).status_code, 422)
        self.db.conn.execute("UPDATE cookies SET user_id=2 WHERE id='a'")
        self.db.conn.commit()
        self.assertEqual(self.client.put(endpoint, json=row, headers=self.auth).status_code, 404)
        self.assertEqual(self.client.delete(endpoint, params={'revision': 1}, headers=self.auth).status_code, 404)

    def test_duplicate_create_conflict_does_not_overwrite_existing_answer(self):
        first = self.client.post('/ai-knowledge/qa', json=self.body, headers=self.auth).json()['entry']
        self.assertEqual(self.client.post('/ai-knowledge/qa', json={**self.body, 'content': '不能覆盖'}, headers=self.auth).status_code, 409)
        self.assertEqual(self.knowledge.list_entries(1), [first])
