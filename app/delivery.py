"""把台账交付给业务侧时的版本说明。

交付以稳定身份组织：``row_id``、``column_id``、``revision``。显示名称、
列位置和行顺序只随附为元数据，绝不参与拼接。

对每一行，交付说明会明确：

* 每个业务字段组（金额、审核状态、凭证）各自来自哪个修订和哪份来源摘要；
* 哪些组确实共享同一修订（``coherent``），哪些跨修订（``split``）；
* 缺失哪些组，因而仍待补领（``pending``）；
* 已删除或未在整表快照中出现的行，作为历史证据保留并单列，不混入活动行。
"""
from __future__ import annotations

from .sheet import ABSENT_FROM_SNAPSHOT, DELETED, Ledger

# 业务字段 -> 约定使用的列 ID。列 ID 稳定；显示名称不参与匹配。
AMOUNT_COLUMN = 'amount'
APPROVAL_STATUS_COLUMN = 'approval_status'
VOUCHER_COLUMN = 'voucher_url'

FIELD_GROUPS = {
    'amount': (AMOUNT_COLUMN,),
    'approval': (APPROVAL_STATUS_COLUMN,),
    'voucher': (VOUCHER_COLUMN,),
}
SPEC_COLUMNS = frozenset(
    column for columns in FIELD_GROUPS.values() for column in columns
)


def _evidence_for(ledger, row_id, column_id):
    return ledger.rows[row_id]['cell_evidence'].get(column_id)


def _other_columns(ledger, row):
    return [
        column_id
        for column_id in sorted(set(ledger.columns) | set(row['cells']))
        if column_id and column_id not in SPEC_COLUMNS
    ]


def _cohorts(groups, approval_state):
    """按修订聚拢确实同版本的字段组，供业务侧直接使用。"""
    by_revision = {}
    for group, payload in groups.items():
        revision = payload['revision']
        if revision is None:
            continue
        by_revision.setdefault(revision, []).append(group)
    pending_groups = [group for group, payload in groups.items()
                      if payload['revision'] is None]
    cohorts = [
        {'revision': revision, 'groups': sorted(members)}
        for revision, members in sorted(by_revision.items())
    ]
    if approval_state is not None:
        cohorts.append({
            'revision': approval_state['revision'],
            'groups': ['approval_state'],
        })
    if pending_groups:
        cohorts.append({'revision': None, 'groups': sorted(pending_groups)})
    return cohorts


def build_manifest(ledger: Ledger):
    """生成交付清单。

    返回::

        {
          'sheet_id', 'head_revision', 'cursor', 'cursor_source',
          'columns': {...},                 # 仅元数据
          'rows': [{
              'row_id', 'groups': {group: {value, revision, evidence, columns}},
              'other': {column_id: {value, revision, evidence}},
              'approval_state': {status, revision, evidence} | None,
              'revisions': [...], 'coherent': bool, 'split_groups': [...],
          }],
          'pending': [{row_id, missing_groups, split_groups, reason}],
          'deleted': [{row_id, status_revision, evidence}],
          'absent': [{row_id, status_revision, evidence}],
          'checkpoints': [...],
          'history': [observations...],
        }

    ``coherent`` 为真表示所有已到达的业务字段组来自同一修订，可以整组使用；
    为假时 ``split_groups`` 列出跨修订的组，业务侧应等待补领后再对账。
    """
    rows = []
    pending = []
    deleted = []
    absent = []

    for row_id in ledger.row_order:
        row = ledger.rows[row_id]
        if row['status'] == DELETED:
            deleted.append({
                'row_id': row_id,
                'status_revision': row['status_revision'],
                'status_evidence': row.get('status_evidence'),
                'last_cells': dict(row['cells']),
                'last_revisions': dict(row['cell_revisions']),
            })
            continue
        if row['status'] == ABSENT_FROM_SNAPSHOT:
            absent.append({
                'row_id': row_id,
                'status_revision': row['status_revision'],
                'status_evidence': row.get('status_evidence'),
                'last_cells': dict(row['cells']),
                'last_revisions': dict(row['cell_revisions']),
            })
            continue

        groups = {}
        missing = []
        group_revisions = {}
        internal_split = []
        for group, columns in FIELD_GROUPS.items():
            present = {
                column_id: row['cell_revisions'].get(column_id)
                for column_id in columns
                if row['cell_revisions'].get(column_id) is not None
            }
            if not present or len(present) != len(columns):
                missing.append(group)
                continue
            distinct = set(present.values())
            if len(distinct) != 1:
                internal_split.append(group)
            group_revisions[group] = (
                next(iter(distinct)) if len(distinct) == 1 else None)
            groups[group] = {
                'columns': {
                    column_id: {
                        'value': row['cells'].get(column_id),
                        'revision': revision,
                        'evidence': _evidence_for(ledger, row_id, column_id),
                    }
                    for column_id, revision in present.items()
                },
                'revision': group_revisions[group],
            }

        other = {}
        for column_id in _other_columns(ledger, row):
            revision = row['cell_revisions'].get(column_id)
            if revision is None and row['cells'].get(column_id) is None:
                continue
            other[column_id] = {
                'value': row['cells'].get(column_id),
                'revision': revision,
                'evidence': _evidence_for(ledger, row_id, column_id),
            }

        approval = row.get('approval')
        approval_state = dict(approval) if approval is not None else None

        # 一致性判定针对三个业务字段组；审核动作是独立的历史事件，
        # 不参与“哪些取值同版本”的判定，只用于 same_revision 说明。
        revisions = {
            revision for revision in group_revisions.values()
            if revision is not None
        }
        head = max(revisions) if revisions else None
        # 跨组版本不一致时，未跟上最新修订的组标记为 split，便于业务侧补领
        split_groups = list(internal_split)
        if len(revisions) > 1 and head is not None:
            for group, revision in group_revisions.items():
                if group in split_groups:
                    continue
                if revision is not None and revision < head:
                    split_groups.append(group)
        split_groups.sort()

        coherent = not missing and not split_groups
        all_revisions = set(revisions)
        if approval_state is not None:
            all_revisions.add(approval_state['revision'])
        entry = {
            'row_id': row_id,
            'groups': groups,
            'other': other,
            'approval_state': approval_state,
            'revisions': sorted(all_revisions),
            'coherent': coherent,
            # 所有业务字段组与审核动作是否恰好来自同一修订
            'same_revision': coherent and len(all_revisions) == 1,
            'split_groups': split_groups,
            # 明确说明哪几组字段确实属于同一版本
            'cohorts': _cohorts(groups, approval_state),
        }
        rows.append(entry)
        if missing or split_groups:
            reasons = []
            if missing:
                reasons.append('missing:' + ','.join(sorted(missing)))
            if split_groups:
                reasons.append('mixed:' + ','.join(sorted(split_groups)))
            pending.append({
                'row_id': row_id,
                'missing_groups': sorted(missing),
                'split_groups': split_groups,
                'reason': ';'.join(reasons),
                'revisions': sorted(all_revisions),
            })

    return {
        'sheet_id': ledger.sheet_id,
        'head_revision': ledger.head_revision,
        'cursor': ledger.cursor,
        'cursor_source': ledger.cursor_source,
        'columns': dict(ledger.columns),
        'rows': rows,
        'pending': pending,
        'deleted': deleted,
        'absent': absent,
        'checkpoints': list(ledger.checkpoints),
        'history': [item.as_dict() for item in ledger.observations],
    }
