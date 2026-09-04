import base64
import json
import unittest

from app.xianyu_im import parse_conversation, parse_message


class XianyuImParserTests(unittest.TestCase):
    def conversation(self, extension, decoded=None):
        message = {'extension': extension, 'content': {'custom': {'summary': '快给ta一个评价吧～'}}}
        if decoded is not None:
            message['content']['custom']['data'] = json.dumps(decoded)
        return {'singleChatConversation': {'cid': 'chat@goofish', 'pairFirst': 'buyer@goofish', 'pairSecond': 'seller@goofish'},
                'lastMessage': {'message': message}}

    def test_system_title_never_becomes_buyer_name(self):
        for decoded in [None, {'contentType': 6, 'title': '快给ta一个评价吧～'},
                        {'contentType': 1, 'text': {'text': '快给ta一个评价吧～'}}]:
            raw = self.conversation({'senderUserId': 'buyer@goofish', 'reminderTitle': '快给ta一个评价吧～'}, decoded)
            self.assertEqual(parse_conversation(raw, 'seller')['otherUserName'], '')
            self.assertEqual(parse_message(raw['lastMessage'], 'seller')['senderName'], '')
        raw = self.conversation({'senderUserId': 'buyer', 'reminderTitle': '其他系统卡片标题'}, {'contentType': 6, 'title': '平台卡片'})
        self.assertEqual(parse_conversation(raw, 'seller')['otherUserName'], '')

    def test_explicit_nick_precedes_title_but_must_belong_to_buyer(self):
        raw = self.conversation({'senderUserId': 'buyer@goofish', 'senderNick': '真实买家', 'reminderTitle': '快给ta一个评价吧～'})
        self.assertEqual(parse_conversation(raw, 'seller')['otherUserName'], '真实买家')
        self.assertEqual(parse_message(raw['lastMessage'], 'seller')['senderName'], '真实买家')
        raw['lastMessage']['message']['extension']['senderUserId'] = 'seller@goofish'
        self.assertEqual(parse_conversation(raw, 'seller')['otherUserName'], '')

    def test_numeric_missing_and_content_copy_titles_are_not_names(self):
        for title in ['12345', '', '和消息一样']:
            raw = self.conversation({'senderUserId': 'buyer', 'reminderTitle': title},
                                    {'contentType': 1, 'text': {'text': '和消息一样'}})
            self.assertEqual(parse_conversation(raw, 'seller')['otherUserName'], '')

    def test_parse_conversation(self):
        payload = base64.b64encode(
            json.dumps({"contentType": 1, "text": {"text": "你好"}}).encode("utf-8")
        ).decode("utf-8")
        result = parse_conversation(
            {
                "singleChatConversation": {
                    "cid": "chat-1@goofish",
                    "pairFirst": "buyer-1@goofish",
                    "pairSecond": "seller-1@goofish",
                    "extension": json.dumps({
                        "itemId": "item-1",
                        "itemTitle": "测试商品",
                    }),
                },
                "lastMessage": {
                    "message": {
                        "content": {"custom": {"data": payload}},
                        "extension": {
                            "senderUserId": "buyer-1@goofish",
                            "reminderTitle": "买家",
                        },
                    }
                },
                "modifyTime": 123456,
                "redPoint": 2,
            },
            "seller-1",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["cid"], "chat-1")
        self.assertEqual(result["otherUserId"], "buyer-1")
        self.assertEqual(result["otherUserName"], "买家")
        self.assertEqual(result["lastMessageSummary"], "你好")
        self.assertEqual(result["itemId"], "item-1")

    def test_parse_text_message(self):
        payload = base64.b64encode(
            json.dumps({"contentType": 1, "text": {"text": "测试消息"}}).encode("utf-8")
        ).decode("utf-8")
        result = parse_message(
            {
                "message": {
                    "messageId": "message-1",
                    "createAt": 123456,
                    "extension": {
                        "senderUserId": "seller-1@goofish",
                        "reminderTitle": "卖家",
                    },
                    "content": {"custom": {"data": payload}},
                }
            },
            "seller-1",
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["isSelf"])
        self.assertEqual(result["type"], "text")
        self.assertEqual(result["text"], "测试消息")


if __name__ == "__main__":
    unittest.main()
