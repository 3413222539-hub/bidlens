import pytest

from package_subcontract import answer_subcontract, standalone_subcontract_question, subcontract_question


def test_subcontract_rule_does_not_take_over_compound_question():
    assert not standalone_subcontract_question("合同履行多久？允许分包吗？投标有效期从哪天起算？")
    assert standalone_subcontract_question("采购包1允许合同分包吗？")


def row(text, page=7):
    return {'evidence_id': f'E{page}', 'page': page, 'row': 1, 'type': 'table', 'text': text}


@pytest.mark.parametrize(('package', 'status', 'expected'), [
    (1, 'answered', '不允许'),
    (2, 'answered', '采购包 2允许'),
    (3, 'needs_review', ''),
])
def test_package_choice_overrides_general_contract(package, status, expected):
    units = [
        row('非主体、非关键性工作分包：采购包1：☑不允许□允许；采购包2：□不允许☑允许；采购包3：□不允许□允许'),
        {'evidence_id': 'E51', 'page': 51, 'row': None, 'type': 'text',
         'text': '未经甲方书面同意，乙方不得分包。'},
    ]
    answer = answer_subcontract(f'采购包{package}能否分包？', units)
    assert answer['status'] == status
    if expected:
        assert expected in answer['answer']
        assert answer['sources'][0]['page'] == 7


def test_conflicting_package_choices_require_review():
    answer = answer_subcontract('采购包1能否分包？', [
        row('非主体工作分包：采购包1：☑允许'),
        row('非主体工作分包：采购包1：☑不允许', 8),
    ])
    assert answer['status'] == 'needs_review'
    assert {source['page'] for source in answer['tool_evidence']} == {7, 8}


def test_core_work_and_followup_scope():
    assert not subcontract_question('采购包2的核心工作能否分包？')
    answer = answer_subcontract('那可以分包吗？', [
        row('非主体工作分包：采购包1：☑不允许；采购包2：☑允许'),
    ], '采购包2有什么要求？')
    assert '采购包 2允许' in answer['answer']
    assert '采购包 1' not in answer['answer']


def test_explicit_project_wide_checkbox():
    answer = answer_subcontract('本项目能否分包？', [
        row('11 | 非主体、非关键性工作分包 | √不允许 □允许', 13),
    ])
    assert '本项目不允许' in answer['answer']
    assert answer['sources'][0]['page'] == 13


def test_allowed_package_retains_its_conditions_only():
    answer = answer_subcontract('采购包1能否分包？', [
        row('非主体工作分包：采购包1：□不允许☑允许，分包给一家中小企业且金额不低于30%。采购包2：☑允许，比例不低于40%。'),
    ])
    assert '一家中小企业且金额不低于30%' in answer['answer']
    assert '40%' not in answer['answer']
