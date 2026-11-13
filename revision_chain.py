"""Chronological, evidence-backed changes; conflicts never silently win."""
import copy
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby

from document_compare import (LIMIT, DEADLINE, QUALIFICATION, LICENSE_NAME, REQUIREMENT,
    REMOVAL, NON_QUALIFICATION, comparison_packages, read_clauses, original_deadlines,
    original_package_limits, compare_documents, citations, project_ids, values)
from package_review import yuan


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def effective_time(value):
    when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise ValueError('生效时间必须带时区')
    return when.astimezone(timezone.utc).isoformat(timespec='seconds')


def initial_state(facts, original, document_id):
    packages, _, limits, _, _ = facts
    numeric = [(set() if p is None else {p}, (yuan(amount), amount), sources)
               for sources, amounts in limits for p, amount in amounts.items()]
    numeric.extend(original_package_limits(original[0]))
    state = {LIMIT: numeric, DEADLINE: original_deadlines(*original), QUALIFICATION: []}
    for field in (LIMIT, DEADLINE):
        expanded = []
        for scope, value, sources in state[field]:
            targets = scope or (packages if field == DEADLINE or len(packages) == 1 else {None})
            for target in targets:
                expanded.append(({target} if target is not None else set(), value,
                                 citations(sources, document_id, '原招标文件')))
        state[field] = expanded
    for text, source in read_clauses(*original):
        names = set(LICENSE_NAME.findall(text))
        if names and REQUIREMENT.search(text) and not (NON_QUALIFICATION.search(text) or REMOVAL.search(text)):
            scope = comparison_packages(text)
            for target in scope or (packages if len(packages) == 1 else {None}):
                state[QUALIFICATION].append(({target} if target is not None else set(), names,
                    citations([source], document_id, '原招标文件')[0]))
    return state


def change_key(row):
    return row['field'], row['package'], (
        tuple(sorted(set(LICENSE_NAME.findall(row['before'] + row['after']))))
        if row['field'] == QUALIFICATION else ())


def apply_state(state, row):
    field, package = row['field'], row['package']
    scope = {package} if package is not None else set()
    if field in (LIMIT, DEADLINE):
        state[field] = [old for old in state[field] if old[0] != scope]
        state[field].append((scope, values(field, row['after'])[0], row['correction_sources']))
    else:
        prior = set(LICENSE_NAME.findall(row['before']))
        state[field] = [(s, n, ref) for s, n, ref in state[field] if not (s == scope and n & prior)]
        if row.get('action') != '删除':
            for ref in row['correction_sources'][:1]:
                state[field].append((scope, set(LICENSE_NAME.findall(row['after'])),
                                     {**ref, 'text': row['after']}))


def compare_chain(facts, original, document_id, notices, loader, *, as_of=None):
    now = effective_time(as_of) if as_of else datetime.now(timezone.utc).isoformat(timespec='seconds')
    state = initial_state(facts, original, document_id)
    ordered = sorted(notices, key=lambda n: (effective_time(n['effective_at']), n['document_id']))
    timeline, latest, blocked, versions = [], {}, set(), {}
    original_ids = project_ids(original)
    for instant, group in groupby(ordered, lambda n: effective_time(n['effective_at'])):
        batch = []
        for notice in group:
            nid = notice['document_id']
            event = {**notice, 'effective_at': instant, 'changes': []}
            timeline.append(event)
            if instant > now:
                event['status'] = 'scheduled'
                continue
            correction = loader(nid)
            other_ids = project_ids(correction)
            if original_ids and other_ids and original_ids.isdisjoint(other_ids):
                event.update(status='needs_review', reason='项目编号不同，未应用此公告。')
                continue
            rows = compare_documents(facts, original, correction, [LIMIT, DEADLINE, QUALIFICATION],
                                     set(), document_id, nid, effective=state)['changes']
            event.update(status='evaluated', changes=rows)
            if not rows:
                event.update(status='needs_review', reason='未识别到支持的变更字段，需核对原文。')
            for row in rows:
                row.update(notice_id=nid, effective_at=instant)
                batch.append((row, event, fingerprint(correction)))
        groups = defaultdict(list)
        for entry in batch:
            groups[change_key(entry[0])].append(entry)
        for key, entries in groups.items():
            conflict = len({(r['after'], r.get('action')) for r, _, _ in entries}) > 1
            for row, event, content_hash in entries:
                if conflict or key in blocked:
                    row.update(status='needs_review', reason='同一生效时间存在冲突，或此前冲突尚未解决。')
                if row['status'] != 'changed':
                    blocked.add(key)
                    event['status'] = 'needs_review'
                    continue
                row['chain_revision'] = fingerprint([versions.get(key), row, content_hash])
                versions[key] = row['chain_revision']
                apply_state(state, row)
                latest[key] = copy.deepcopy(row)
    return {'status': 'comparison', 'changes': [r for e in timeline for r in e['changes']],
            'timeline': timeline, 'current': list(latest.values()), 'as_of': now,
            'has_conflicts': any(e['status'] == 'needs_review' for e in timeline)}
