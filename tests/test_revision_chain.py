import pytest
from pathlib import Path
from types import SimpleNamespace

import team_store
import web_app
from document_compare import LIMIT, DEADLINE, QUALIFICATION
from package_review import read_package_facts
from revision_chain import compare_chain, fingerprint
from test_document_compare import evidence

ORIGINAL = evidence((1, '最高限价：第1包50万元；第2包75万元'),
                    (2, '投标截止时间：2026年9月22日14时30分'),
                    (3, '标段2须具有《食品经营许可证》。'))


def run(notices, times=None):
    times = times or [f'2026-09-{i+1:02}T10:00:00+08:00' for i in range(len(notices))]
    entries = [{'document_id': str(i), 'effective_at': t} for i,t in enumerate(times)]
    return compare_chain(read_package_facts(None, ORIGINAL), ORIGINAL, 'original', entries[::-1],
                         lambda key: notices[int(key)], as_of='2026-11-13T00:00:00+08:00')


def test_three_revisions_use_effective_values_not_original_or_upload_order():
    result = run([evidence((1, f'第2包最高限价由{old}万元调整为{new}万元。'))
                  for old,new in [(75,80),(80,85),(85,90)]])
    assert [r['before'] for r in result['changes']] == ['75万元','80万元','85万元']
    assert all(r['status'] == 'changed' for r in result['changes'])
    assert result['current'][0]['after'] == '90万元'
    assert result['changes'][1]['original_sources'][0]['document_id'] == '0'


def test_conflicting_same_time_and_bad_previous_value_do_not_overwrite():
    notices = [evidence((1, f'第2包最高限价由75万元调整为{new}万元。')) for new in (80,85)]
    result = run(notices, ['2026-09-01T00:00:00Z'] * 2)
    assert result['current'] == [] and result['has_conflicts']
    result = run([notices[0], evidence((1, '第2包最高限价由75万元调整为90万元。'))])
    assert result['current'][0]['after'] == '80万元'
    assert result['changes'][1]['status'] == 'needs_review'


def test_future_notice_and_cross_project_are_not_applied():
    notice = evidence((1,'第2包最高限价由75万元调整为80万元。'))
    result = run([notice], ['2099-01-01T00:00:00Z'])
    assert result['timeline'][0]['status'] == 'scheduled' and not result['current']


def test_reconfirmation_history_keeps_previous_revision_and_other_package():
    result = run([evidence((1,'第2包最高限价由75万元调整为80万元。')),
                  evidence((1,'第2包最高限价由80万元调整为85万元。'))])
    from package_review import package_checklist
    facts = read_package_facts(None, ORIGINAL)
    report = {'items': [], 'candidate_count': 0}
    first = package_checklist(report, facts, 2)
    old = {'by':'甲', 'at':'2026-09-01T00:00:00Z'}
    second = {'by':'乙', 'at':'2026-09-02T00:00:00Z'}
    web_app.apply_chain_to_checklist(first, {**result, 'timeline':result['timeline'][:1]}, 2, {'price-limit': old})
    previous_id = next(i for i in first['items'] if i.get('change_revision'))['id']
    final = package_checklist(report, facts, 2)
    confirmed = {'price-limit': old, previous_id: second}
    web_app.apply_chain_to_checklist(final, result, 2, confirmed)
    item = next(i for i in final['items'] if i.get('change_revision'))
    assert item['id'] not in confirmed and item['previous_confirmation'] == second
    assert item['revision_history'][1]['previous_confirmation'] == second
    assert len(item['id']) < 200
    unaffected = package_checklist(report, facts, 1)
    web_app.apply_chain_to_checklist(unaffected, result, 1, {})
    assert not any(i.get('change_revision') for i in unaffected['items'])


def test_revision_list_optimistic_lock_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(team_store, 'DB', tmp_path / 'team.sqlite3')
    empty = team_store.revisions('doc')
    first = team_store.revisions('doc', [{'document_id':'a', 'effective_at':'2026-01-01T00:00:00Z'}], empty['version'], '甲')
    with pytest.raises(ValueError, match='刷新'):
        team_store.revisions('doc', [], empty['version'], '乙')
    assert team_store.revisions('doc') == first
    with team_store.connect() as db:
        assert db.execute('SELECT reviewer FROM revision_events').fetchone()[0] == '甲'


def test_qualification_change_can_be_followed_by_removal():
    result = run([evidence((1,'第2包须具有《食品经营许可证》更正为须提供《食品经营许可证》和检验报告。')),
                  evidence((1,'第2包取消《食品经营许可证》资格要求。'))])
    assert all(r['status'] == 'changed' for r in result['changes'])
    assert result['changes'][-1]['action'] == '删除'
    assert result['changes'][-1]['original_sources'][0]['document_id'] == '0'
