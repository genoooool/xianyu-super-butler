import json
import unittest
from unittest.mock import patch

from app.ai_reply_engine import AIReplyEngine
from app.services.human_handoff import HandoffReply
from app.services.conversation_safety import conversation_answer


class NormalConversationTests(unittest.TestCase):
    def generate(self, question, answer, *, history=None, bargains=0, enabled=True, knowledge=None):
        engine = AIReplyEngine()
        settings = dict(ai_enabled=enabled, context_enabled=True, max_bargain_rounds=3, custom_prompts='亲切简短。')
        with patch.object(engine, 'is_ai_enabled', return_value=enabled), \
             patch.object(engine, 'save_conversation'), \
             patch.object(engine, '_get_recent_user_messages', return_value=[]), \
             patch.object(engine, 'get_conversation_context', return_value=history or []), \
             patch.object(engine, 'get_bargain_count', return_value=bargains), \
             patch.object(engine, '_generate_with_retry', return_value=answer) as model, \
             patch('app.ai_reply_engine.db_manager.get_ai_reply_settings', return_value=settings), \
             patch('app.ai_reply_engine.KnowledgeService.for_reply', return_value=knowledge or []) as lookup:
            result = engine.generate_reply(question, {'title': '10元100星只是错误标题', 'price': 1}, 'chat', 'a', 'buyer', 'item', True)
        return result, model, lookup

    def test_normal_conversation_is_not_limited_to_greeting_keywords(self):
        for question, reply in [('你好', '你好呀，请问有什么可以帮您？'), ('我先想想，晚点再来', '好的，慢慢考虑，有问题随时聊。'),
                                ('谢谢你耐心跟我说这么多', '不客气，很高兴能帮到您。'), ('我不太会表达', '没关系，慢慢说，我在听。')]:
            with self.subTest(question=question):
                result, model, _ = self.generate(question, json.dumps(dict(kind='conversation', reply=reply), ensure_ascii=False))
                self.assertEqual(result, reply)
                model.assert_called_once()
                prompt = model.call_args.args[1][0]['content']
                self.assertNotIn('10元100星', prompt)
                self.assertNotIn('商品价格: 1', prompt)

    def test_obvious_business_or_mixed_question_cannot_bypass_by_saying_hello(self):
        for question in ['你好，多少钱', '你好！现货可拍吗', '10元100个对吧', '今天能发货吗', '包退款吗', '保证能用吗', '167']:
            result, model, _ = self.generate(question, '{"kind":"conversation","reply":"没问题"}', bargains=9)
            self.assertIsInstance(result, HandoffReply, question)
            model.assert_not_called()

    def test_implicit_business_uses_model_context_and_handoffs(self):
        history = [dict(role='user', content='可以明天送达吗'), dict(role='assistant', content='需要确认')]
        result, model, _ = self.generate('那就这么定了？', '{"kind":"needs_facts","reply":""}', history=history)
        self.assertIsInstance(result, HandoffReply)
        self.assertIn(history[0], model.call_args.args[1])

    def test_bad_model_contract_and_hallucinated_claims_fail_closed(self):
        for answer in ['你好', '{bad}', '[]', '{"kind":"conversation","reply":true}',
                       '{"kind":"conversation","reply":"你好，现货可拍"}',
                       '{"kind":"conversation","reply":"今天发货"}',
                       '{"kind":"conversation","reply":"10元100个"}',
                       '{"kind":"conversation","reply":"保证没问题"}',
                       '{"kind":"conversation","reply":"__IMAGE_SEND__"}',
                       '{"kind":"conversation","reply":"__HUMAN_HANDOFF__"}']:
            result, _, _ = self.generate('你好', answer)
            self.assertIsInstance(result, HandoffReply, answer)

    def test_disabled_ai_never_calls_model_or_knowledge(self):
        result, model, lookup = self.generate('你好', '', enabled=False)
        self.assertIsNone(result)
        model.assert_not_called(); lookup.assert_not_called()

    def test_knowledge_answers_keep_existing_scoped_grounding_path(self):
        result, model, lookup = self.generate('怎么使用', '按说明激活即可。', knowledge=[
            dict(id=1, topic='使用', source='商品资料', content='按说明激活即可。')])
        self.assertEqual(result, '按说明激活即可。')
        lookup.assert_called_once_with('a', 'item', '怎么使用')

    def test_extra_fields_and_overlong_answers_are_rejected_without_truncating(self):
        self.assertIsNone(conversation_answer(json.dumps(dict(kind='conversation', reply='可以', override=True))))
        self.assertIsNone(conversation_answer(json.dumps(dict(kind='conversation', reply='好'*301))))


if __name__ == '__main__':
    unittest.main()
