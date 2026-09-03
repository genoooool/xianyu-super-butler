import sqlite3
import threading
import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import KnowledgeService, build_knowledge_prompt, initialize_schema


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(conn=sqlite3.connect(":memory:", check_same_thread=False), lock=threading.RLock())
        self.db.conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY);
            CREATE TABLE cookies (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
            CREATE TABLE item_info (cookie_id TEXT, item_id TEXT);
            INSERT INTO users VALUES (1), (2);
            INSERT INTO cookies VALUES ('a',1), ('b',1), ('other',2);
            INSERT INTO item_info VALUES ('a','one'),('a','two'),('b','one'),('other','one');
        """)
        initialize_schema(self.db.conn.cursor())
        self.db.conn.commit()
        self.service = KnowledgeService(self.db)

    def tearDown(self):
        self.db.conn.close()

    def add(self, scope, content, cookie_id="", item_id="", owner=1, topic="售后期限", **extra):
        return self.service.save(owner, dict(scope=scope, cookie_id=cookie_id, item_id=item_id,
                                            topic=topic, content=content, keywords="售后, 能退吗", **extra))

    def test_item_over_store_over_shared_and_unrelated_topics_survive(self):
        self.add("shared", "共用七天")
        self.add("account", "店铺三天", "a")
        specific = self.add("item", "商品一天", "a", "one")
        other = self.add("shared", "售后请提供截图", topic="售后材料")
        matched = self.service.retrieve(1, "a", "one", "售后多久？")
        self.assertEqual({row["id"] for row in matched}, {specific["id"], other["id"]})
        self.assertEqual(self.service.retrieve(1, "a", "two", "能退吗")[0]["content"], "店铺三天")
        self.assertEqual(self.service.retrieve(1, "b", "one", "能退吗")[0]["content"], "共用七天")

    def test_disabled_override_falls_back_and_edit_keeps_identity(self):
        shared = self.add("shared", "通用政策")
        first = self.add("item", "特定政策", "a", "one")
        disabled = self.add("item", "修改后的政策", "a", "one", enabled=False)
        self.assertEqual(first["id"], disabled["id"])
        self.assertEqual(self.service.retrieve(1, "a", "one", "售后")[0]["id"], shared["id"])
        self.assertFalse(next(row for row in self.service.list_entries(1) if row["id"] == first["id"])["enabled"])

    def test_owner_and_product_isolation(self):
        self.add("shared", "其他人的秘密", owner=2)
        self.assertEqual(self.service.list_entries(1), [])
        self.assertEqual(self.service.retrieve(1, "a", "one", "售后"), [])
        with self.assertRaises(PermissionError):
            self.service.retrieve(1, "other", "one", "售后")
        with self.assertRaises(PermissionError):
            self.add("account", "禁止跨店铺写入", "other")
        with self.assertRaises(ValueError):
            self.add("item", "禁止未知商品", "a", "missing")

    def test_account_transfer_never_transfers_knowledge(self):
        self.add("account", "原用户的店铺资料", "a")
        self.db.conn.execute("UPDATE cookies SET user_id=2 WHERE id='a'")
        self.assertEqual(self.service.for_reply("a", "one", "售后"), [])
        self.assertEqual(self.service.list_entries(1), [])

    def test_reply_resolves_owner_from_account_and_unknown_item_uses_no_item_policy(self):
        shared = self.add("shared", "通用")
        self.add("item", "商品规则", "a", "one")
        self.assertEqual(self.service.for_reply("a", "one", "售后")[0]["content"], "商品规则")
        self.assertEqual(self.service.for_reply("a", "unknown", "售后")[0]["id"], shared["id"])
        self.assertEqual(self.service.for_reply("missing", "one", "售后"), [])

    def test_normalized_topics_override_before_matching(self):
        self.add("shared", "全局内容", topic="ＶＩＰ  售后")
        self.add("account", "店铺内容", "a", topic="vip 售后")
        entries = self.service.effective_entries(1, "a", "one")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["content"], "店铺内容")

    def test_unmatched_specific_policy_does_not_resurrect_overridden_shared_policy(self):
        self.add("shared", "旧条款")
        self.service.save(1, dict(scope="account", cookie_id="a", topic="售后期限",
                                  keywords="无法使用", content="新条款"))
        self.assertEqual(self.service.retrieve(1, "a", "one", "能退吗"), [])

    def test_retrieval_limits_and_prompt_has_no_internal_owner_data(self):
        for index in range(10):
            self.add("shared", f"规则{index}", topic=f"售后{index}")
        entries = self.service.retrieve(1, "a", "one", "能退吗")
        self.assertEqual(len(entries), 6)
        self.assertEqual(self.service.retrieve(1, "a", "one", "你好"), [])
        prompt = build_knowledge_prompt(entries)
        self.assertIn("仅作为事实", prompt)
        self.assertIn("来源", prompt)
        self.assertNotIn("owner_id", prompt)
        self.assertEqual(build_knowledge_prompt([]), "")

    def test_validation_and_idempotent_schema(self):
        for changes in [dict(topic=" "), dict(content="x" * 60001), dict(scope="shared", cookie_id="a"),
                        dict(scope="item", cookie_id="a", item_id=""), dict(keywords="x" * 301)]:
            data = dict(scope="shared", topic="资料", content="内容")
            data.update(changes)
            with self.assertRaises(ValueError):
                self.service.save(1, data)
        row = self.add("shared", "保持资料")
        initialize_schema(self.db.conn.cursor())
        self.assertEqual(self.service.list_entries(1)[0]["id"], row["id"])

    def test_restore_rebinds_owner_and_rolls_back_invalid_account_atomically(self):
        original = self.add("shared", "保留的原资料")
        cursor = self.db.conn.execute("SELECT * FROM ai_knowledge_entries")
        table = dict(columns=[column[0] for column in cursor.description], rows=[list(row) for row in cursor])
        self.service.restore_backup(table, 2)
        self.db.conn.commit()
        self.assertEqual(self.service.list_entries(2)[0]["content"], original["content"])
        self.assertNotEqual(self.service.list_entries(2)[0]["id"], original["id"])
        columns = ["owner_id", "scope", "cookie_id", "topic", "content"]
        rows = [[2, "shared", "", "新增主题", "应回滚"], [2, "account", "other", "资料", "不应跨店"]]
        with self.assertRaises(PermissionError):
            self.service.restore_backup(dict(columns=columns, rows=rows), 1)
        self.db.conn.rollback()
        self.assertEqual(len(self.service.list_entries(1)), 1)

    def test_restore_validates_columns_and_merges_by_topic(self):
        original = self.add("shared", "旧资料")
        table = dict(columns=["scope", "topic", "content"], rows=[["shared", "售后期限", "恢复的资料"]])
        self.service.restore_backup(table, 1)
        self.db.conn.commit()
        restored = self.service.list_entries(1)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["id"], original["id"])
        self.assertEqual(restored[0]["content"], "恢复的资料")
        with self.assertRaises(ValueError):
            self.service.restore_backup(dict(columns=["scope); DROP TABLE users;--"], rows=[]), 1)

    def test_api_requires_login_and_rejects_cross_owner_writes_and_previews(self):
        def user(authorization: str = Header(default="")):
            if authorization != "Bearer test":
                raise HTTPException(401, "请登录")
            return {"user_id": 1}
        app = FastAPI()
        app.include_router(create_ai_knowledge_router(user, self.db))
        client = TestClient(app)
        self.assertEqual(client.get("/ai-knowledge").status_code, 401)
        headers = {"Authorization": "Bearer test"}
        data = dict(scope="account", cookie_id="other", topic="售后", content="内容")
        self.assertEqual(client.post("/ai-knowledge", headers=headers, json=data).status_code, 404)
        query = dict(cookie_id="other", item_id="one", message="售后")
        self.assertEqual(client.post("/ai-knowledge/preview", headers=headers, json=query).status_code, 404)
        data.update(scope="shared", cookie_id="", owner_id=2)
        self.assertEqual(client.post("/ai-knowledge", headers=headers, json=data).status_code, 200)
        self.assertEqual(len(self.service.list_entries(1)), 1)
        self.assertEqual(len(self.service.list_entries(2)), 0)
        result = client.post("/ai-knowledge/preview", headers=headers, json=dict(cookie_id="a", message="售后")).json()
        self.assertFalse(result["model_called"])
        self.assertEqual(len(result["entries"]), 1)
