"""把来源响应接入修订流台账。

三类响应分别处理：

``changes``   增量变更事件，按 revision / row_id / column_id 追加；
``view``      筛选视图，只补充可见证据，隐藏行保持原状（不是删除）；
``snapshot``  整表快照，用于游标过期后的重新对齐，可衔接已有的新修订。

任何取值都只按 ``row_id`` 和 ``column_id`` 定位，显示列名、列位置和行在
响应中的顺序只作为元数据存档。
"""
from __future__ import annotations

import copy

from .sheet import (
    ABSENT_FROM_SNAPSHOT,
    DELETED,
    Ledger,
    Observation,
    _integer,
    _text,
    loads_strict,
)


def _body(value):
    if not isinstance(value, dict):
        raise ValueError('来源实体不是对象')
    payload = value.get('sheet')
    return payload if isinstance(payload, dict) else value


def _columns(raw):
    if raw is None:
        return {}
    if isinstance(raw, list):
        mapping = {}
        for entry in raw:
            column_id = _text(entry, 'column_id', '列身份')
            meta = {k: v for k, v in entry.items() if k != 'column_id'}
            mapping[column_id] = meta
        return mapping
    if not isinstance(raw, dict):
        raise ValueError('列定义无效')
    return raw


def _row_cells(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError('单元格集合无效')
    cells = {}
    for column_id, value in raw.items():
        if not isinstance(column_id, str) or not column_id:
            raise ValueError('列身份无效')
        cells[column_id] = value
    return cells


def _rows(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('行集合无效')
    return raw


def _revision(payload, label='表格版本'):
    return _integer(payload, 'revision', label)


def apply_response(ledger, response, body=None):
    """校验并应用一次来源响应，返回本次响应产生的观察列表。

    响应要么完整应用，要么完全不改变台账：先在副本上应用，成功后
    才提交。这样一页中的某条无效事件不会留下半套证据，也不会因
    摘要已登记而无法重新领取。同一响应（摘要相同）重复输入是幂等的。
    """
    candidate = copy.deepcopy(ledger)
    applied = _apply_response(candidate, response, body)
    ledger.__dict__.update(candidate.__dict__)
    return applied


def _apply_response(ledger, response, body=None):
    if response.status != 200:
        raise ValueError('来源未返回成功实体')
    sha = response.sha256
    existing = ledger.evidences.get(sha)
    if existing is not None:
        if existing.source_id != response.source_id:
            raise ValueError('同一摘要来自不同来源')
        return []
    if body is None:
        text = response.body.decode('utf-8')
        body = loads_strict(text)
    payload = _body(body)
    kind = _text(payload, 'kind', '响应类型')
    if kind not in ('changes', 'view', 'snapshot', 'cursor_expired'):
        raise ValueError('未知响应类型')
    ledger.add_evidence(
        source_id=response.source_id,
        url=response.url,
        status=response.status,
        sha256=sha,
        kind=kind,
        revision=payload.get('revision'),
    )
    if kind == 'cursor_expired':
        return _cursor_expired(ledger, payload, response, sha)
    if kind == 'snapshot':
        return _snapshot(ledger, payload, response, sha)
    if kind == 'view':
        return _view(ledger, payload, response, sha)
    return _changes(ledger, payload, response, sha)


def _bind_sheet(ledger, payload):
    sheet_id = payload.get('sheet_id')
    if sheet_id is None:
        return
    if not isinstance(sheet_id, str) or not sheet_id:
        raise ValueError('表格身份无效')
    if ledger.sheet_id and ledger.sheet_id != sheet_id:
        raise ValueError('响应属于另一张表格')
    ledger.sheet_id = sheet_id


def _cursor_of(payload):
    return payload.get('cursor', payload.get('next_cursor'))


def _observe(ledger, *, revision, operation, row_id, column_id, value,
             evidence, source_id, basis):
    return ledger.record(Observation(
        revision=revision,
        operation=operation,
        row_id=row_id,
        column_id=column_id,
        value=value,
        evidence=evidence,
        source_id=source_id,
        basis=basis,
    ))


def _changes(ledger, payload, response, sha):
    _bind_sheet(ledger, payload)
    page_revision = _revision(payload)
    ledger.require_fresh(page_revision)
    raw_events = payload.get('changes', payload.get('events'))
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ValueError('变更事件无效')
    applied = []
    seen = set()
    for event in raw_events:
        event_revision = _integer(event, 'revision', '变更版本')
        # 一页可以带回多个修订的事件，但它们都不晚于响应的表格版本
        if event_revision > page_revision:
            raise ValueError('变更版本晚于响应版本')
        operation = _text(event, 'operation', '变更操作')
        if operation not in ('set', 'clear', 'delete', 'approve', 'revoke_approval'):
            raise ValueError('未知变更操作')
        row_id = _text(event, 'row_id', '变更行身份')
        column_id = event.get('column_id')
        if operation in ('set', 'clear'):
            if not isinstance(column_id, str) or not column_id:
                raise ValueError('变更列身份无效')
        elif column_id is not None:
            raise ValueError('该操作不应携带列身份')
        identity = (event_revision, operation, row_id, column_id)
        if identity in seen:
            raise ValueError('同一响应重复声明同一变更')
        seen.add(identity)
        _apply_event(ledger, event_revision, operation, row_id, column_id,
                     event.get('value'), sha, response.source_id)
        applied.append(_observe(
            ledger,
            revision=event_revision,
            operation=operation,
            row_id=row_id,
            column_id=column_id,
            value=event.get('value'),
            evidence=sha,
            source_id=response.source_id,
            basis='changes',
        ))
    cursor = _cursor_of(payload)
    if cursor is not None:
        ledger.add_checkpoint(
            revision=page_revision,
            cursor=cursor,
            cursor_source=response.source_id,
            evidence=sha,
            kind='changes',
        )
    return applied


def _apply_event(ledger, revision, operation, row_id, column_id, value,
                 sha, source_id):
    if operation == 'set':
        ledger.set_cell(row_id, column_id, value, revision, evidence=sha)
    elif operation == 'clear':
        ledger.clear_cell(row_id, column_id, revision, evidence=sha)
    elif operation == 'delete':
        ledger.mark_deleted(row_id, revision, evidence=sha)
    elif operation == 'approve':
        ledger.set_approval(row_id, 'approved', revision, evidence=sha)
    elif operation == 'revoke_approval':
        # 撤销审核是新的历史事实，旧审批观察仍然保留
        ledger.set_approval(row_id, 'revoked', revision, evidence=sha)


def _view(ledger, payload, response, sha):
    _bind_sheet(ledger, payload)
    revision = _revision(payload)
    ledger.require_fresh(revision)
    filter_id = payload.get('filter_id')
    if filter_id is not None and not isinstance(filter_id, str):
        raise ValueError('筛选视图身份无效')
    applied = []
    for entry in _rows(payload.get('rows')):
        row_id = _text(entry, 'row_id', '行身份')
        cells = _row_cells(entry.get('cells'))
        ledger.ensure_row(row_id)
        for column_id, value in cells.items():
            ledger.ensure_column(column_id)
            # 视图值只作为可见证据，记录版本与来源，不改变删除状态
            ledger.set_cell(row_id, column_id, value, revision, evidence=sha)
            applied.append(_observe(
                ledger,
                revision=revision,
                operation='set',
                row_id=row_id,
                column_id=column_id,
                value=value,
                evidence=sha,
                source_id=response.source_id,
                basis=f'view:{filter_id}' if filter_id else 'view',
            ))
    cursor = _cursor_of(payload)
    if cursor is not None:
        ledger.add_checkpoint(
            revision=revision,
            cursor=cursor,
            cursor_source=response.source_id,
            evidence=sha,
            kind='view',
            filter_id=filter_id,
        )
    return applied


def _snapshot(ledger, payload, response, sha):
    _bind_sheet(ledger, payload)
    revision = _revision(payload)
    previous_head = ledger.head_revision
    # 快照用于重新对齐，但不得把已采到的更晚修订当作“未发生”。
    # 只能把旧快照内容作为证据补充进来；晚于快照版本的证据继续有效。
    if previous_head is not None and revision < previous_head:
        raise ValueError('快照版本早于已采修订')
    snapshot_of = {
        'previous_head_revision': previous_head,
        'previous_head_evidence': ledger.head_evidence,
        'previous_cursor': ledger.cursor,
        'previous_cursor_source': ledger.cursor_source,
        'previous_evidence': sorted(sha_ for sha_ in ledger.evidences if sha_ != sha),
        'reason': payload.get('reason'),
    }
    columns = _columns(payload.get('columns'))
    if columns:
        ledger.replace_columns(columns)
    applied = []
    seen_rows = set()
    for entry in _rows(payload.get('rows')):
        row_id = _text(entry, 'row_id', '行身份')
        if row_id in seen_rows:
            raise ValueError('快照重复声明同一行')
        seen_rows.add(row_id)
        row = ledger.ensure_row(row_id)
        # 更晚的整表快照若重新包含该行，则当前状态恢复为活动；
        # 早先的删除观察仍完整保留在历史中，不会被抹去。
        if row['status'] in ('deleted', ABSENT_FROM_SNAPSHOT):
            applied.append(_observe(
                ledger,
                revision=revision,
                operation='restore_from_snapshot',
                row_id=row_id,
                column_id=None,
                value=row['status'],
                evidence=sha,
                source_id=response.source_id,
                basis='snapshot',
            ))
        ledger.mark_active(row_id, revision, evidence=sha)
        cells = _row_cells(entry.get('cells'))
        for column_id, value in cells.items():
            ledger.ensure_column(column_id)
            # 早于已采修订的快照已被拒绝，因此这里的值不会回退证据
            ledger.set_cell(row_id, column_id, value, revision, evidence=sha)
            applied.append(_observe(
                ledger,
                revision=revision,
                operation='set',
                row_id=row_id,
                column_id=column_id,
                value=value,
                evidence=sha,
                source_id=response.source_id,
                basis='snapshot',
            ))
    for row_id, row in list(ledger.rows.items()):
        if row_id in seen_rows:
            continue
        if row['status'] == DELETED:
            continue
        if row['status_revision'] is not None and row['status_revision'] > revision:
            # 更晚的修订已就该行给出结论，旧快照的缺席不能覆盖它
            continue
        ledger.mark_absent(row_id, revision, evidence=sha)
        applied.append(_observe(
            ledger,
            revision=revision,
            operation='absent_from_snapshot',
            row_id=row_id,
            column_id=None,
            value=None,
            evidence=sha,
            source_id=response.source_id,
            basis='snapshot',
        ))
    cursor = _cursor_of(payload)
    if cursor is not None:
        ledger.add_checkpoint(
            revision=revision,
            cursor=cursor,
            cursor_source=response.source_id,
            evidence=sha,
            kind='snapshot',
            snapshot_of=snapshot_of,
        )
    return applied


def _cursor_expired(ledger, payload, response, sha):
    """游标过期本身不产生业务变更，只记录对齐前的衔接依据。

    过期的游标不推进台账游标，避免把旧游标误标成新版本。
    """
    _bind_sheet(ledger, payload)
    revision = payload.get('revision')
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError('表格版本无效')
    head = ledger.head_revision
    if revision is not None and head is not None and revision < head:
        raise ValueError('修订版本回退')
    ledger.checkpoints.append({
        'revision': revision if revision is not None else head,
        'cursor': ledger.cursor,
        'cursor_source': ledger.cursor_source,
        'evidence': sha,
        'kind': 'cursor_expired',
        'expired_cursor': payload.get('cursor', ledger.cursor),
        'head_revision': head,
        'head_evidence': ledger.head_evidence,
        'reason': payload.get('reason') or 'expired_cursor',
    })
    return []
