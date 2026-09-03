"""Bound text input without a tokenizer dependency or cutting safety rules.

UTF-8 bytes are a conservative size proxy, NOT an exact model token count.
Unknown/custom models can have smaller windows; provider errors still fail closed.
"""
from app.services.ai_knowledge import build_knowledge_prompt

MAX_INPUT_BYTES = 20_000
MAX_KNOWLEDGE_BYTES = 9_000


def input_size(messages):
    return sum(len(message["content"].encode("utf-8")) + 64 for message in messages)


def build_bounded_messages(system_prompt, safety_prompt, knowledge, history, question):
    base = [{"role": "system", "content": system_prompt + safety_prompt},
            {"role": "user", "content": question}]
    if input_size(base) > MAX_INPUT_BYTES:
        # Never clip a price, negation, system constraint or current question.
        raise ValueError("商品/提示词/当前问题超过输入预算，需要精简或人工处理")
    selected = []
    for entry in knowledge:
        candidate = build_knowledge_prompt([*selected, entry])
        if len(candidate.encode("utf-8")) <= min(MAX_KNOWLEDGE_BYTES, MAX_INPUT_BYTES - input_size(base)):
            selected.append(entry)
    if knowledge and not selected:
        raise ValueError("匹配的知识资料无法放入输入预算，需要精简或人工处理")
    base[0]["content"] = system_prompt + build_knowledge_prompt(selected) + safety_prompt
    recent = []
    for message in reversed(history):
        if input_size([base[0], message, *recent, base[1]]) > MAX_INPUT_BYTES:
            break  # Keep a contiguous recent suffix, not detached old exchanges.
        recent.insert(0, message)
    return [base[0], *recent, base[1]]
