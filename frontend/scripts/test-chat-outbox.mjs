// Execute the real pure TS merge function without adding a test dependency.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../services/chatOutbox.ts', import.meta.url), 'utf8');
const exports = {};
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports });
const merge = exports.mergeChatMessages;
const message = (id, self = true) => ({ messageId: id, isSelf: self, text: '你好', images: [], time: 1 });
const local = { ...message('local'), accountId: 'a', cid: 'chat', receiptIds: ['server-text', 'server-image'], status: 'sent' };
const history = [message('server-text'), message('server-image'), message('another-same-text'), message('incoming', false)];
assert.equal(merge(history, [local], 'a', 'chat').length, 3);
assert.equal(merge([], [local], 'a', 'chat').length, 1, 'Lagging histories must not hide a sent bubble');
assert.equal(merge(history, [local], 'b', 'chat').length, 4, 'Same cid in another store stays isolated');
assert.equal(merge(history, [local], 'a', 'other').length, 4);
assert.equal(merge([message('server-text', false)], [local], 'a', 'chat').length, 2, 'Never fold incoming content');
assert.equal(merge(history, [{ ...local, receiptIds: [], status: 'unconfirmed' }], 'a', 'chat').length, 5,
  'Without a reliable ID, never guess which history message succeeded');
assert.equal(history.length, 4, 'Do not mutate server history');
const many = Array.from({ length: 100 }, (_, index) => ({ ...local, messageId: `local-${index}` }));
const bounded = exports.trimConfirmedOutbox([{ ...local, messageId: 'unknown', status: 'unconfirmed' }, ...many]);
assert.equal(bounded.length, 51);
assert.equal(bounded[0].messageId, 'unknown', 'Never discard an unresolved payload to enforce the sent-cache bound');
console.log('9 chat outbox assertions passed');
