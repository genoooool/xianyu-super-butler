import unittest
from unittest.mock import patch
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from tests import test_ai_knowledge
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.knowledge_documents import knowledge_chunks, preview_document
from app.services.ai_context_budget import MAX_INPUT_BYTES, build_bounded_messages, input_size


class DocumentTests(unittest.TestCase):
    setUp = test_ai_knowledge.KnowledgeTests.setUp
    tearDown = test_ai_knowledge.KnowledgeTests.tearDown
    add = test_ai_knowledge.KnowledgeTests.add
    def test_import_preview_is_lossless_text_and_bounded(self):
        text = '# 售后\n支持换货。\n\n<script>alert(1)</script>'
        result = preview_document('../policy.MD', ('\ufeff' + text).encode())
        self.assertEqual(result['filename'], 'policy.MD')
        self.assertEqual(result['content'], text)
        self.assertEqual(self.service.list_entries(1), [])
        for name, raw in [('a.pdf', b'test'), ('a.txt', b'\xff'), ('a.md', b'\0hi'),
                          ('a.txt', b' '), ('a.txt', b'a' * 262145), ('a.md', ('文' * 60001).encode())]:
            with self.assertRaises(ValueError):
                preview_document(name, raw)

    def test_long_document_retrieves_relevant_tail_and_whole_topic_override(self):
        content = '# 简介\n' + ('资料介绍无特殊含义。\n' * 300) + '\n# 激活说明\n激活失败时使用设备重启功能。'
        shared = self.add('shared', content, topic='说明书')
        matched = self.service.retrieve(1, 'a', 'one', '激活失败设备重启')
        self.assertIn('激活失败', matched[0]['content'])
        self.assertEqual(matched[0]['id'], shared['id'])
        self.assertTrue(all(len(row['content']) <= 2000 for row in matched))
        self.service.save(1, dict(scope='item', cookie_id='a', item_id='one', topic='说明书', content='商品专属的新规定'))
        self.assertEqual(self.service.retrieve(1, 'a', 'one', '激活失败设备重启'), [])
        self.assertIn('激活失败', self.service.retrieve(1, 'a', 'two', '激活失败设备重启')[0]['content'])

    def test_import_collision_and_total_quota_do_not_mutate(self):
        self.add('shared', '原资料')
        before = self.service.list_entries(1)
        with self.assertRaises(FileExistsError):
            self.service.save(1, dict(scope='shared', topic='售后期限', content='覆盖'), create_only=True)
        self.assertEqual(self.service.list_entries(1), before)
        with patch('app.services.ai_knowledge.MAX_OWNER_CHARS', 5):
            with self.assertRaises(ValueError):
                self.add('shared', '另一个资料', topic='新主题')
            self.add('shared', '替换')  # Replacement subtracts its previous size.

    def test_long_unbroken_text_is_not_lost(self):
        value = '甲乙丙' * 20000
        self.assertEqual(''.join(knowledge_chunks(value)), value)
        self.assertLessEqual(max(map(len, knowledge_chunks(value))), 2000)

    def test_document_api_requires_auth_and_explicit_confirm(self):
        def user(authorization: str = Header(default='')):
            if authorization != 'Bearer test': raise HTTPException(401)
            return {'user_id': 1}
        app = FastAPI(); app.include_router(create_ai_knowledge_router(user, self.db))
        with TestClient(app) as client:
            path = '/ai-knowledge/documents/preview?filename=guide.md'
            self.assertEqual(client.post(path, content=b'test').status_code, 401)
            headers = {'Authorization': 'Bearer test', 'Content-Type': 'application/octet-stream'}
            preview = client.post(path, content='测试文档'.encode(), headers=headers)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(self.service.list_entries(1), [])
            self.assertEqual(client.post(path, content=b'a' * 262145, headers=headers).status_code, 413)
            headers.pop('Content-Type')
            body = dict(scope='account', cookie_id='other', topic='guide', content='资料')
            self.assertEqual(client.post('/ai-knowledge/documents/import', json=body, headers=headers).status_code, 404)
            body['cookie_id'] = 'a'
            self.assertEqual(client.post('/ai-knowledge/documents/import', json=body, headers=headers).status_code, 200)
            self.assertEqual(client.post('/ai-knowledge/documents/import', json=body, headers=headers).status_code, 409)


class ContextBudgetTests(unittest.TestCase):
    def test_long_history_and_knowledge_are_bounded_without_cutting_facts(self):
        safety = '不得低于100元；不能证明已付款。'
        facts = [dict(source='商品专属', topic=f'规则{i}', content='完整资料；' * 300) for i in range(6)]
        history = [dict(role='user' if i % 2 == 0 else 'assistant', content=str(i) + '历史' * 800) for i in range(30)]
        messages = build_bounded_messages('系统', safety, facts, history, '当前问题')
        self.assertLessEqual(input_size(messages), MAX_INPUT_BYTES)
        self.assertTrue(messages[0]['content'].endswith(safety))
        self.assertEqual(messages[-1], {'role': 'user', 'content': '当前问题'})
        self.assertIn(facts[0]['content'], messages[0]['content'])
        self.assertLess(len(messages), len(history) + 2)
        self.assertEqual(messages[-2], history[-1])

    def test_oversized_required_facts_fail_closed(self):
        for args in [('系统' * 10000, '', [], [], '问题'), ('系统', '事实' * 10000, [], [], '问题'),
                     ('系统', '底价100', [], [], '问题' * 10000)]:
            with self.assertRaises(ValueError): build_bounded_messages(*args)

    def test_small_request_is_unchanged(self):
        history = [dict(role='user', content='你好')]
        self.assertEqual(build_bounded_messages('系统', '安全', [], history, '问题'),
                         [dict(role='system', content='系统安全'), *history, dict(role='user', content='问题')])

    def test_required_facts_are_not_silently_dropped_for_space(self):
        with self.assertRaises(ValueError):
            build_bounded_messages('x' * 19000, '安全', [dict(source='商品专属', topic='规则', content='规定' * 500)], [], '问题')
