from paper_agent.proposal_view import model_overview


def test_current_model_sections_exclude_background_and_code_headings():
    proposal = '''# Proposal
## 背景
Do not show this
## 模型输入
X [b, 100, 3]
## 模型架构
```text
X → encoder → y
## not a real heading
```
Shared encoder.
### Update
Replay buffer.
## 模型输出
One lifetime scalar.
## 授权
Do not show this either
'''
    result = model_overview(proposal)
    assert result['input'] == 'X [b, 100, 3]'
    assert result['output'] == 'One lifetime scalar.'
    assert 'X → encoder → y' in result['diagram']
    assert 'Replay buffer.' in result['architecture']
    assert 'Do not show' not in str(result)


def test_legacy_model_uses_real_tensor_and_prediction_definitions():
    result = model_overview('''### 系统概述
```text
输入 → MLP → 寿命
```
#### 1. 输入/输出、寿命定义
Prediction 预测总寿命，派生RUL。
#### 2. 表示与精确架构
三个通道：V、I、Q。

单电芯 X [100,3,300]；批量 [b,100,3,300]。

| 模块 | 输出 |
|---|---|
| MLP | [b,1] |
#### 3. 评价
Hidden evaluation
''')
    assert result['diagram'] == '输入 → MLP → 寿命'
    assert '批量 [b,100,3,300]' in result['input']
    assert '派生RUL' in result['output']
    assert 'Hidden evaluation' not in str(result)


def test_missing_model_is_not_fabricated():
    assert model_overview('# Title\n## Background\nUnrelated') == dict(diagram='', input='', architecture='', output='')


def test_structured_expert_consent_is_not_a_revision():
    from paper_agent.agent.workflow_cli import is_review_approval
    questions = ['采用共享编码器？', '按推荐输出？']
    assert is_review_approval('问题：采用共享编码器？\n回答：按你的建议来\n\n问题：按推荐输出？\n回答：同意', questions)
    assert not is_review_approval('问题：采用共享编码器？\n回答：同意，但宽度改为64\n\n问题：按推荐输出？\n回答：同意', questions)
    assert not is_review_approval('问题：采用共享编码器？\n回答：同意', questions)
