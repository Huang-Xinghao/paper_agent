import json
from types import SimpleNamespace

import pytest

from paper_agent.api import intent


@pytest.mark.parametrize("text", ["你是谁？", "你好", "Who are you?"])
def test_basic_chat_does_not_need_a_model(text, monkeypatch):
    monkeypatch.setattr(intent, "ModelClient", lambda **kw: pytest.fail("no model needed"))
    assert intent.route_message(text)["intent"] == "chat"


@pytest.mark.parametrize("decision", [
    {"intent": "chat", "reply": "过拟合是模型对训练样本拟合过度。"},
    {"intent": "clarify", "reply": "想研究什么主题？"},
    {"intent": "research", "research_prompt": "调研电池寿命预测"},
])
def test_model_routing_uses_context_without_tools(decision, monkeypatch):
    def complete(**kwargs):
        assert "tools" not in kwargs
        assert kwargs["timeout"] == 45
        assert kwargs["messages"][-3:] == [
            {"role": "user", "content": "帮我研究"},
            {"role": "assistant", "content": "什么主题？"},
            {"role": "user", "content": "电池寿命预测"},
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(decision)))])
    monkeypatch.setattr(intent, "ModelClient", lambda **kw: SimpleNamespace(
        model="fake", client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))))
    assert intent.route_message("电池寿命预测", [{"prompt": "帮我研究", "reply": "什么主题？"}]) == decision


@pytest.mark.parametrize("content", ['{}', '{"intent":"research"}', '{"intent":"chat","reply":[]}', 'not json'])
def test_invalid_model_response_is_rejected(content, monkeypatch):
    monkeypatch.setattr(intent, "ModelClient", lambda **kw: SimpleNamespace(model="fake", client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))))))
    with pytest.raises(ValueError):
        intent.route_message("帮我研究")
