"""Offline only: real SQLite/services/caller code, fake classifier and platform receipts."""
import ast
import asyncio
import base64
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import KnowledgeService, initialize_schema
from app.services.fixed_replies import FixedReplies, CLARIFY_REPLY
from app.services.quick_phrases import QuickPhrases, initialize_schema as phrases_schema
from app.services.reply_assets import ReplyAssets, MAX_IMAGE_BYTES
from app.services.reply_delivery import send_parts, ReplyDeliveryError, require_receipt
from app.services.human_handoff import initialize_schema as handoff_schema


def png():
    output = io.BytesIO()
    Image.new('RGB', (12, 8), 'yellow').save(output, format='PNG')
    return output.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(conn=sqlite3.connect(':memory:', check_same_thread=False), lock=threading.RLock())
        self.db.conn.executescript('''
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE cookies(id TEXT PRIMARY KEY,user_id INTEGER);
            CREATE TABLE item_info(cookie_id TEXT,item_id TEXT);
            INSERT INTO users VALUES(1),(2);
            INSERT INTO cookies VALUES('a',1),('b',1),('other',2);
            INSERT INTO item_info VALUES('a','one'),('a','two'),('b','one'),('other','one');
            CREATE TABLE chat_quick_phrases(id INTEGER PRIMARY KEY,category TEXT DEFAULT '默认',title TEXT,content TEXT,
                sort_order INTEGER DEFAULT 0,enabled INTEGER DEFAULT 1,use_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        initialize_schema(self.db.conn.cursor()); phrases_schema(self.db.conn.cursor()); handoff_schema(self.db.conn.cursor()); self.db.conn.commit()
        self.knowledge = KnowledgeService(self.db)
        self.fixed = FixedReplies(self.db)
        self.assets = ReplyAssets(self.db)
        self.phrases = QuickPhrases(self.db)
        self.receipt = dict(headers=dict(code=200, mid='test-receipt'), body={})

    def tearDown(self):
        self.db.conn.close()

    def rule(self, **changes):
        return self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='客户询价', keywords='多少钱，怎么卖',
            content='点开商品购买页面下面就有对应的价格哦亲。', entry_type='qa', **changes))

    def update(self, rule, **changes):
        return self.knowledge.save(1, {**rule, **changes})


class FixedSelectionTests(Fixture):
    def test_exact_literal_answer_does_not_call_model(self):
        rule = self.rule()
        self.update(rule, content='  10元10个\n只按此说明。  ')
        classifier = Mock(side_effect=AssertionError('must not call'))
        result = self.fixed.choose('a', 'one', '多少钱？', classifier)
        self.assertEqual(result.rule['content'], '  10元10个\n只按此说明。  ')
        self.assertEqual(result.reason, 'keyword'); classifier.assert_not_called()

    def test_intent_selects_id_not_answer_or_image(self):
        asset = self.assets.save(1, png())
        rule = self.rule(image_ids=[asset['id']])
        prompts = []
        def classify(messages):
            prompts.extend(messages)
            return json.dumps(dict(status='match', id=rule['id']))
        result = self.fixed.choose('a', 'one', '你页面上不是10元10个星吗', classify)
        self.assertEqual(result.rule, rule)
        packed = json.dumps(prompts, ensure_ascii=False)
        self.assertNotIn(rule['content'], packed); self.assertNotIn(asset['id'], packed)
        self.assertNotIn('assistant', packed)

    def test_bad_or_injected_classifier_output_never_becomes_answer(self):
        self.rule()
        for output in ['10元100个', '__IMAGE_SEND__https://example.invalid/a.png', None,
                       '{"status":"match","id":9999}', '{"status":"match","id":true}',
                       '{"status":"match","id":1,"answer":"1元可拍"}', '```json\n{}\n```',
                       '{"status":"none","id":1}', '{"status":"match","id":1,"id":2}', '[]']:
            with self.subTest(output=output):
                result = self.fixed.choose('a', 'one', '怎么收费呢', lambda _: output)
                self.assertEqual(result.status, 'clarify'); self.assertIsNone(result.rule)

    def test_negated_greeting_is_not_substring_match(self):
        row = self.rule(); self.update(row, topic='问候', keywords='你好', content='固定问候')
        model = Mock(return_value='{"status":"none","id":null}')
        self.assertEqual(self.fixed.choose('a', 'one', '我不是来问你好的，我问价格', model).status, 'no_match')
        model.assert_called_once()

    def test_ambiguous_keywords_clarify(self):
        self.rule(match_mode='contains')
        self.knowledge.save(1, dict(scope='account', cookie_id='a', topic='发货', keywords='发货', content='固定发货', entry_type='qa', match_mode='contains'))
        result = self.fixed.choose('a', 'one', '多少钱，何时发货')
        self.assertEqual(result.status, 'clarify')

    def test_scope_override_before_matching_and_other_store_isolation(self):
        data = dict(scope='shared', topic='报价', content='共用', keywords='多少钱', entry_type='qa')
        self.knowledge.save(1, data)
        self.knowledge.save(1, {**data, 'scope':'account', 'cookie_id':'a', 'content':'店铺'})
        self.knowledge.save(1, {**data, 'scope':'item', 'cookie_id':'a', 'item_id':'one', 'content':'单商品', 'keywords':'怎么卖'})
        self.assertEqual(self.fixed.choose('a','one','怎么卖').rule['content'], '单商品')
        self.assertEqual(self.fixed.choose('a','one','多少钱').status, 'no_match')
        self.assertEqual(self.fixed.choose('a','two','多少钱').rule['content'], '店铺')
        self.assertEqual(self.fixed.choose('b','one','多少钱').rule['content'], '共用')
        self.assertEqual(self.fixed.rules('other','one'), [])

    def test_saved_changes_and_new_override_invalidate_pending_selection(self):
        row = self.rule()
        choice = self.fixed.choose('a','one','多少钱')
        self.assertTrue(self.fixed.revalidate(choice,'a','one'))
        self.update(row, enabled=False)
        self.assertFalse(self.fixed.revalidate(choice,'a','one'))
        self.update(row, enabled=True)
        choice = self.fixed.choose('a','one','多少钱')
        self.knowledge.save(1,{**row,'scope':'item','item_id':'one','content':'新商品规则'})
        self.assertFalse(self.fixed.revalidate(choice,'a','one'))

    def test_qa_never_enters_knowledge_and_knowledge_cannot_overwrite_qa(self):
        row = self.rule()
        self.assertEqual(self.knowledge.retrieve(1,'a','one','多少钱'), [])
        with self.assertRaises(FileExistsError): self.update(row, entry_type='knowledge')
        self.knowledge.save(1,{**row,'scope':'item','item_id':'one','entry_type':'knowledge'})
        self.assertEqual(self.fixed.choose('a','one','多少钱').rule['id'], row['id'])

    def test_unknown_product_and_classifier_failure_fail_closed(self):
        self.rule()
        with self.assertRaises(ValueError): self.fixed.choose('a','missing','多少钱')
        result = self.fixed.choose('a','one','价格呢',Mock(side_effect=TimeoutError))
        self.assertEqual(result.status,'clarify')


class AssetAndPhraseTests(Fixture):
    def test_static_image_validation_dedup_and_owner_isolation(self):
        asset = self.assets.save(1,png())
        self.assertEqual(self.assets.save(1,png())['id'],asset['id'])
        self.assertEqual((asset['width'],asset['height']),(12,8))
        with self.assertRaises(PermissionError): self.assets.get(2,asset['id'])
        for invalid in [b'<svg/>',b'x'*(MAX_IMAGE_BYTES+1),b'']:
            with self.assertRaises(ValueError): self.assets.save(1,invalid)
        for ids in [[asset['id']]*2,['https://example.invalid/a.png']]:
            with self.assertRaises((ValueError,PermissionError)): self.assets.validate(1,ids)

    def test_image_only_save_and_private_image_api(self):
        def user(authorization: str = Header(default='')):
            if authorization not in ('Bearer one','Bearer two'): raise HTTPException(401)
            return {'user_id':1 if authorization=='Bearer one' else 2}
        app=FastAPI(); app.include_router(create_ai_knowledge_router(user,self.db)); client=TestClient(app)
        self.assertEqual(client.post('/ai-knowledge/images',content=png()).status_code,401)
        headers={'Authorization':'Bearer one'}
        asset=client.post('/ai-knowledge/images',headers=headers,content=png()).json()
        result=client.post('/ai-knowledge',headers=headers,json=dict(scope='shared',topic='报价图',entry_type='qa',image_ids=[asset['id']]))
        self.assertEqual(result.status_code,200); self.assertEqual(result.json()['entry']['content'],'')
        response=client.get('/ai-knowledge/images/'+asset['id'],headers=headers)
        self.assertEqual(response.headers['cache-control'],'no-store'); self.assertEqual(response.content,self.assets.get(1,asset['id'])['data'])
        self.assertEqual(client.get('/ai-knowledge/images/'+asset['id'],headers={'Authorization':'Bearer two'}).status_code,404)

    def test_phrase_owned_persistence_image_only_and_update(self):
        asset=self.assets.save(1,png())
        identity=self.phrases.save(1,dict(title='报价',content='',image_ids=[asset['id']]))
        self.assertEqual(self.phrases.list(1)[0]['image_ids'],[asset['id']]); self.assertEqual(self.phrases.list(2),[])
        with self.assertRaises(PermissionError): self.phrases.save(2,dict(enabled=False),identity)
        with self.assertRaises(PermissionError): self.phrases.delete(2,identity)
        self.phrases.save(1,dict(enabled=False),identity)
        self.assertEqual(self.phrases.list(1),[]); self.assertEqual(len(self.phrases.list(1,True)),1)

    def test_backup_roundtrip_rebinds_images_and_rolls_back_atomically(self):
        asset=self.assets.save(1,png()); self.rule(image_ids=[asset['id']])
        self.phrases.save(1,dict(title='报价图',image_ids=[asset['id']]))
        rows=self.assets.export_backup(1); phrases=self.phrases.export_backup(1)
        self.db.conn.execute('BEGIN')
        mapping=self.assets.restore_backup(rows,2)
        self.phrases.restore_backup(phrases,2,mapping)
        rebound=self.phrases.list(2)[0]['image_ids'][0]
        self.assertNotEqual(rebound,asset['id']); self.assets.get(2,rebound)
        self.db.conn.rollback()
        self.assertEqual(self.phrases.list(2),[])
        with self.assertRaises(PermissionError): self.assets.get(2,rebound)


class DeliveryTests(Fixture):
    def setup_delivery(self):
        self.asset=self.assets.save(1,png())
        self.instance=SimpleNamespace(cookies_str='offline',myid='seller',send_im_text=AsyncMock(return_value=self.receipt),_send_im_request=AsyncMock(return_value=self.receipt))
        uploader=SimpleNamespace(upload_reply_bytes=AsyncMock(return_value='https://img.alicdn.com/approved.png'))
        manager=AsyncMock(); manager.__aenter__.return_value=uploader
        self.upload=uploader.upload_reply_bytes
        return patch.dict(sys.modules,{'utils.image_uploader':SimpleNamespace(ImageUploader=Mock(return_value=manager))})

    def test_image_only_exact_payload_and_no_extra_text(self):
        with self.setup_delivery():
            count=asyncio.run(send_parts(self.instance,self.db,1,'chat','buyer','',[self.asset['id']]))
        self.assertEqual(count,1); self.instance.send_im_text.assert_not_called()
        args=self.instance._send_im_request.call_args.args
        content=json.loads(base64.b64decode(args[1][0]['content']['custom']['data']))
        self.assertEqual(content,dict(contentType=2,image=dict(pics=[dict(url='https://img.alicdn.com/approved.png',width=12,height=8,type=0)])))
        self.assertEqual(self.upload.call_args.args[0],self.assets.get(1,self.asset['id'])['data'])

    def test_upload_failure_does_not_send_text_or_retry(self):
        with self.setup_delivery():
            self.upload.side_effect=TimeoutError
            with self.assertRaises(ReplyDeliveryError) as error:
                asyncio.run(send_parts(self.instance,self.db,1,'chat','buyer','报价',[self.asset['id']]))
        self.assertEqual(error.exception.sent_count,0); self.instance.send_im_text.assert_not_called(); self.instance._send_im_request.assert_not_called()
        self.upload.assert_awaited_once()

    def test_pause_after_upload_suppresses_every_send(self):
        with self.setup_delivery():
            check=Mock(side_effect=[True,True,False])
            with self.assertRaises(ReplyDeliveryError):
                asyncio.run(send_parts(self.instance,self.db,1,'chat','buyer','报价',[self.asset['id']],check))
        self.instance.send_im_text.assert_not_called(); self.instance._send_im_request.assert_not_called()

    def test_partial_delivery_is_reported_without_retry(self):
        with self.setup_delivery():
            self.instance._send_im_request.side_effect=TimeoutError
            with self.assertRaises(ReplyDeliveryError) as error:
                asyncio.run(send_parts(self.instance,self.db,1,'chat','buyer','  原文\n10元10个  ',[self.asset['id']]))
        self.assertEqual(error.exception.sent_count,1); self.instance.send_im_text.assert_awaited_once_with('chat','buyer','  原文\n10元10个  ')
        self.instance._send_im_request.assert_awaited_once()

    def test_negative_receipt_and_text_marker_rejected(self):
        for response in [None,{},dict(headers=dict(code=500),body={}),dict(body=dict(reason='denied'))]:
            with self.assertRaises(RuntimeError): require_receipt(response)
        with self.assertRaises(ValueError):
            asyncio.run(send_parts(None,self.db,1,'chat','buyer','__IMAGE_SEND__https://bad',[]))


class ActualCallerTests(Fixture):
    def setUp(self):
        super().setUp()
        from account_control_fixture import initialize_account_control
        initialize_account_control(self.db)

    def run_caller(self, message='多少钱', model=None, paused=None, failure=False, no_qa=False):
        source=Path(__file__).resolve().parents[1]/'XianyuAutoAsync.py'
        tree=ast.parse(source.read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_process_chat_message_reply')
        from app.desktop_updates import update_gate
        env=dict(update_gate=update_gate,asyncio=asyncio,time=time,logger=Mock(),AUTO_REPLY=dict(enabled=True,api=dict(enabled=False)),pause_manager=SimpleNamespace(is_chat_paused=lambda *args: bool(paused and paused[0])))
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),'exec'),env)
        self.db.matches_message_filter=lambda *args: False
        self.db.get_ai_reply_settings=lambda *args: dict(ai_enabled=True)
        instance=SimpleNamespace(cookie_id='a',_add_reply_decision_log=Mock(return_value=1),_update_reply_decision_log=Mock(),_safe_str=str,
            get_keyword_reply=AsyncMock(return_value=None),get_ai_reply=AsyncMock(return_value=None),get_default_reply=AsyncMock(return_value='UNSAFE DEFAULT'),send_msg=AsyncMock(),send_image_msg=AsyncMock())
        sent=[]
        async def send_handoff(chat_id, buyer_id, text):
            if failure: raise RuntimeError('offline failure')
            sent.append((text, []))
            return self.receipt
        instance.send_im_text = AsyncMock(side_effect=send_handoff)
        async def deliver(*args):
            if failure: raise RuntimeError('offline failure')
            if args[-1](): sent.append(args[5:7])
        with patch.dict(sys.modules,{'app.db_manager':SimpleNamespace(db_manager=self.db),'app.ai_reply_engine':SimpleNamespace(ai_reply_engine=SimpleNamespace(_generate_with_retry=model or Mock(return_value='bad output')))}),patch('app.services.reply_delivery.send_parts',side_effect=deliver):
            asyncio.run(env['_process_chat_message_reply'](instance,{'1':{'5':int(time.time()*1000)}},None,'buyer','buyer',message,'one','chat','now'))
        return instance,sent

    def test_actual_caller_sends_stored_answer_and_does_not_fall_through(self):
        row=self.rule(); instance,sent=self.run_caller()
        self.assertEqual(sent,[(row['content'],[])])
        instance.get_ai_reply.assert_not_called(); instance.get_default_reply.assert_not_called()

    def test_actual_caller_failure_has_no_default_fallback(self):
        self.rule(); instance,sent=self.run_caller(failure=True)
        self.assertEqual(sent,[]); instance.get_default_reply.assert_not_called(); instance.send_msg.assert_not_called()

    def test_actual_caller_invalid_classification_clarifies_only(self):
        self.rule(); instance,sent=self.run_caller('10元几个')
        self.assertEqual(sent,[(CLARIFY_REPLY,[])]); instance.get_ai_reply.assert_not_called()

    def test_actual_caller_checks_pause_after_classification(self):
        row=self.rule(); paused=[False]
        def classifier(*args):
            paused[0]=True
            return json.dumps(dict(status='match',id=row['id']))
        instance,sent=self.run_caller('10元几个',classifier,paused)
        self.assertEqual(sent,[]); instance.get_default_reply.assert_not_called()


if __name__=='__main__': unittest.main()
