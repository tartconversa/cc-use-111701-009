"""协作费用表的修订流台账。

台账只以稳定身份保存证据：

* ``row_id``      行身份，不随排序变化；
* ``column_id``   列身份，不随显示名称或位置变化；
* ``revision``    表格版本，不随分页或视图变化。

显示名称、列位置和行在响应中的顺序仅作为元数据记录，从不参与取值、
合并或缺失判断。筛选视图只提供可见证据，隐藏行不会被当作删除。
删除和撤销审核是追加的历史证据，不会被后续修订覆盖。

来源响应仍由 :mod:`app.source` 校验，台账不直接接受裸 JSON。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

KINDS = frozenset({'changes', 'view', 'snapshot', 'cursor_expired'})
OPERATIONS = frozenset({
    'set', 'clear', 'delete', 'approve', 'revoke_approval', 'absent_from_snapshot',
    'restore_from_snapshot',
})

ACTIVE = 'active'
DELETED = 'deleted'
ABSENT_FROM_SNAPSHOT = 'absent_from_snapshot'


def _text(obj, key, label):
    value = obj.get(key) if isinstance(obj, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}无效')
    return value


def _integer(obj, key, label, *, minimum=0):
    value = obj.get(key) if isinstance(obj, dict) else None
    if type(value) is not int or value < minimum:
        raise ValueError(f'{label}无效')
    return value


def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('JSON 对象键重复')
        obj[key] = value
    return obj


def loads_strict(text):
    """拒绝重复键和非标准常量的 JSON 解析，避免同一对象出现两个版本的字段。"""

    def reject(value):
        raise ValueError('来源实体含非标准数值')

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject,
        )
    except json.JSONDecodeError as exc:
        raise ValueError('来源实体不是有效 JSON') from exc


@dataclass(frozen=True)
class Evidence:
    """一次来源响应的可核对身份。"""

    source_id: str
    url: str
    status: int
    sha256: str
    kind: str
    revision: int | None

    def as_dict(self):
        return {
            'source_id': self.source_id,
            'url': self.url,
            'status': self.status,
            'sha256': self.sha256,
            'kind': self.kind,
            'revision': self.revision,
        }


@dataclass(frozen=True)
class Observation:
    """一条追加式证据。"""

    revision: int
    operation: str
    row_id: str
    column_id: str | None
    value: Any
    evidence: str
    source_id: str
    basis: str

    def as_dict(self):
        return {
            'revision': self.revision,
            'operation': self.operation,
            'row_id': self.row_id,
            'column_id': self.column_id,
            'value': self.value,
            'evidence': self.evidence,
            'source_id': self.source_id,
            'basis': self.basis,
        }


def _normalize_row(meta):
    if not isinstance(meta, dict):
        raise ValueError('台账行状态无效')
    status = _text(meta, 'status', '行状态')
    if status not in (ACTIVE, DELETED, ABSENT_FROM_SNAPSHOT):
        raise ValueError('台账行状态无效')
    cells = meta.get('cells') or {}
    cell_revisions = meta.get('cell_revisions') or {}
    if not isinstance(cells, dict) or not isinstance(cell_revisions, dict):
        raise ValueError('台账单元格无效')
    status_revision = meta.get('status_revision')
    if status_revision is not None and (type(status_revision) is not int or status_revision < 0):
        raise ValueError('台账行版本无效')
    status_evidence = meta.get('status_evidence')
    if status_evidence is not None and not isinstance(status_evidence, str):
        raise ValueError('台账行证据无效')
    approval = meta.get('approval')
    if approval is not None:
        if not isinstance(approval, dict):
            raise ValueError('台账审核状态无效')
        if _text(approval, 'status', '审核状态') not in ('approved', 'revoked'):
            raise ValueError('台账审核状态无效')
        if _integer(approval, 'revision', '审核版本') < 0:
            raise ValueError('台账审核版本无效')
    cell_evidence = meta.get('cell_evidence') or {}
    if not isinstance(cell_evidence, dict):
        raise ValueError('台账单元格证据无效')
    return {
        'status': status,
        'status_revision': status_revision,
        'status_evidence': status_evidence,
        'cells': dict(cells),
        'cell_revisions': dict(cell_revisions),
        'cell_evidence': dict(cell_evidence),
        'approval': dict(approval) if approval is not None else None,
    }


def _normalize_cursor(value):
    if value is None or isinstance(value, str):
        return value
    if type(value) is int and value >= 0:
        return value
    raise ValueError('变更游标无效')


@dataclass
class Ledger:
    """一次采集会话内的追加式证据台账。

    台账可以序列化到 JSON，以便跨运行恢复；它从不重写历史观察。
    """

    sheet_id: str = ''
    columns: dict[str, dict] = field(default_factory=dict)
    rows: dict[str, dict] = field(default_factory=dict)
    row_order: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    evidences: dict[str, Evidence] = field(default_factory=dict)
    checkpoints: list[dict] = field(default_factory=list)
    cursor: str | int | None = None
    cursor_source: str | None = None
    cursor_revision: int | None = None

    @classmethod
    def load(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError('台账状态无效')
        ledger = cls(_text(payload, 'sheet_id', '台账 sheet_id'))
        columns = payload.get('columns')
        if not isinstance(columns, dict):
            raise ValueError('台账列定义无效')
        for column_id, meta in columns.items():
            if not isinstance(column_id, str) or not isinstance(meta, dict):
                raise ValueError('台账列定义无效')
            ledger.columns[column_id] = dict(meta)
        rows = payload.get('rows')
        if not isinstance(rows, dict):
            raise ValueError('台账行状态无效')
        for row_id, meta in rows.items():
            if not isinstance(row_id, str):
                raise ValueError('台账行状态无效')
            ledger.rows[row_id] = _normalize_row(meta)
        order = payload.get('row_order', [])
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            raise ValueError('台账行顺序无效')
        ledger.row_order = list(order)
        for raw in payload.get('observations', []):
            operation = _text(raw, 'operation', '观察操作')
            if operation not in OPERATIONS:
                raise ValueError('台账观察操作无效')
            column_id = raw.get('column_id')
            if column_id is not None and (not isinstance(column_id, str) or not column_id):
                raise ValueError('台账观察列身份无效')
            ledger.observations.append(Observation(
                revision=_integer(raw, 'revision', '观察版本'),
                operation=operation,
                row_id=_text(raw, 'row_id', '观察行身份'),
                column_id=column_id,
                value=raw.get('value'),
                evidence=_text(raw, 'evidence', '观察证据'),
                source_id=_text(raw, 'source_id', '观察来源'),
                basis=_text(raw, 'basis', '观察基准'),
            ))
        for sha, raw in (payload.get('evidences') or {}).items():
            ledger.evidences[sha] = Evidence(
                source_id=_text(raw, 'source_id', '证据来源'),
                url=_text(raw, 'url', '证据地址'),
                status=_integer(raw, 'status', '证据状态', minimum=100),
                sha256=_text(raw, 'sha256', '证据摘要'),
                kind=_text(raw, 'kind', '证据类型'),
                revision=raw.get('revision'),
            )
        ledger.checkpoints = list(payload.get('checkpoints') or [])
        ledger.cursor = _normalize_cursor(payload.get('cursor'))
        ledger.cursor_source = payload.get('cursor_source')
        if ledger.cursor_source is not None and not isinstance(ledger.cursor_source, str):
            raise ValueError('台账游标来源无效')
        ledger.cursor_revision = payload.get('cursor_revision')
        if ledger.cursor_revision is not None and (
            type(ledger.cursor_revision) is not int or ledger.cursor_revision < 0
        ):
            raise ValueError('台账游标版本无效')
        return ledger

    def as_dict(self):
        return {
            'sheet_id': self.sheet_id,
            'columns': self.columns,
            'rows': self.rows,
            'row_order': self.row_order,
            'observations': [item.as_dict() for item in self.observations],
            'evidences': {sha: item.as_dict() for sha, item in self.evidences.items()},
            'checkpoints': self.checkpoints,
            'cursor': self.cursor,
            'cursor_source': self.cursor_source,
            'cursor_revision': self.cursor_revision,
        }

    def save(self, path):
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(self.as_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')

    @property
    def head_revision(self):
        """已采到的最高版本。

        游标过期信号报告的是表格当前版本，但它没有采到任何行证据，
        因此不推进“已采修订”，只保留在 checkpoints 里作为衔接依据。
        """
        revisions = [item.revision for item in self.observations if item.revision is not None]
        revisions.extend(
            item['revision'] for item in self.checkpoints
            if item.get('revision') is not None and item.get('kind') != 'cursor_expired'
        )
        return max(revisions) if revisions else None

    @property
    def head_evidence(self):
        """最近一次已采修订对应的来源摘要，用于快照衔接。"""
        head = self.head_revision
        if head is None:
            return None
        for item in reversed(self.observations):
            if item.revision == head:
                return item.evidence
        for item in reversed(self.checkpoints):
            if item.get('revision') == head and item.get('kind') != 'cursor_expired':
                return item.get('evidence')
        return None

    def add_evidence(self, *, source_id, url, status, sha256, kind, revision):
        item = Evidence(source_id, url, status, sha256, kind, revision)
        existing = self.evidences.get(sha256)
        if existing is not None and existing != item:
            raise ValueError('同一摘要对应两组来源元数据')
        self.evidences[sha256] = item
        return item

    def record(self, observation):
        if observation.operation not in OPERATIONS:
            raise ValueError('未知变更操作')
        self.observations.append(observation)
        return observation

    def require_fresh(self, revision):
        """拒绝回退到已经应用过的版本，避免旧数据覆盖新证据。"""
        head = self.head_revision
        if head is not None and revision < head:
            raise ValueError('修订版本回退')

    def replace_columns(self, mapping):
        """整表快照携带的列定义；显示名称与位置只记录，不用于身份。"""
        if not isinstance(mapping, dict):
            raise ValueError('列定义无效')
        for column_id, meta in mapping.items():
            if not isinstance(column_id, str) or not column_id:
                raise ValueError('列身份无效')
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                raise ValueError('列定义无效')
            stored = dict(meta)
            stored.pop('column_id', None)
            self.ensure_column(column_id, name=stored.get('name'), position=stored.get('position'))
            stored.pop('name', None)
            stored.pop('position', None)
            if stored:
                self.columns[column_id].update(stored)

    # -- 状态推进 ---------------------------------------------------------
    def ensure_column(self, column_id, *, name=None, position=None):
        existing = self.columns.get(column_id)
        meta = existing or {}
        meta = dict(meta)
        if name is not None:
            meta['name'] = name
        if position is not None:
            meta['position'] = position
        # 显示名称/位置只作元数据；列身份始终是 column_id
        self.columns[column_id] = meta
        for row_id, row in self.rows.items():
            row['cells'].setdefault(column_id, None)

    def ensure_row(self, row_id):
        row = self.rows.get(row_id)
        if row is None:
            row = {
                'status': ACTIVE,
                'status_revision': None,
                'status_evidence': None,
                'cells': {column_id: None for column_id in self.columns},
                'cell_revisions': {},
                'cell_evidence': {},
                'approval': None,
            }
            self.rows[row_id] = row
            self.row_order.append(row_id)
        return row

    def set_cell(self, row_id, column_id, value, revision, evidence=None):
        row = self.ensure_row(row_id)
        self.ensure_column(column_id)
        prior = row['cell_revisions'].get(column_id)
        if prior is not None and revision < prior:
            raise ValueError('单元格版本回退')
        if prior == revision and row['cells'].get(column_id) != value:
            raise ValueError('同一版本出现冲突的单元格值')
        row['cells'][column_id] = value
        row['cell_revisions'][column_id] = revision
        row['cell_evidence'][column_id] = evidence

    def clear_cell(self, row_id, column_id, revision, evidence=None):
        row = self.ensure_row(row_id)
        prior = row['cell_revisions'].get(column_id)
        if prior is not None and revision < prior:
            raise ValueError('单元格版本回退')
        row['cells'][column_id] = None
        row['cell_revisions'][column_id] = revision
        row['cell_evidence'][column_id] = evidence

    def mark_deleted(self, row_id, revision, evidence=None):
        row = self.ensure_row(row_id)
        prior = row['status_revision']
        if prior is not None and revision < prior:
            raise ValueError('行状态版本回退')
        row['status'] = DELETED
        row['status_revision'] = revision
        row['status_evidence'] = evidence

    def mark_absent(self, row_id, revision, evidence=None):
        row = self.ensure_row(row_id)
        if row['status'] == DELETED:
            return
        prior = row['status_revision']
        if prior is not None and revision < prior:
            raise ValueError('行状态版本回退')
        row['status'] = ABSENT_FROM_SNAPSHOT
        row['status_revision'] = revision
        row['status_evidence'] = evidence

    def mark_active(self, row_id, revision, evidence=None):
        """由整表快照恢复行；删除观察仍留在历史中。

        同一修订里既删除又出现是来源矛盾，必须拒绝。
        """
        row = self.ensure_row(row_id)
        prior = row['status_revision']
        if prior is not None and revision < prior:
            raise ValueError('行状态版本回退')
        if row['status'] == DELETED and prior == revision:
            raise ValueError('同一版本删除与恢复冲突')
        row['status'] = ACTIVE
        row['status_revision'] = revision
        row['status_evidence'] = evidence

    def set_approval(self, row_id, status, revision, *, evidence=None):
        row = self.ensure_row(row_id)
        prior = row['approval']
        if prior is not None and revision < prior.get('revision', -1):
            raise ValueError('审核状态版本回退')
        row['approval'] = {
            'status': status,
            'revision': revision,
            'evidence': evidence,
        }

    def add_checkpoint(self, *, revision, cursor, cursor_source, evidence, kind='changes', **flow):
        """记录游标与修订的衔接点。

        整表快照会带 ``snapshot_of``：此前已采到的 head 版本、旧游标与其来源、
        过期信号等，作为“旧证据 -> 新快照”的衔接依据，而不是丢弃旧证据。
        """
        checkpoint = {
            'revision': revision,
            'cursor': cursor,
            'cursor_source': cursor_source,
            'evidence': evidence,
            'kind': kind,
        }
        checkpoint.update(flow)
        self.checkpoints.append(checkpoint)
        self.cursor = cursor
        self.cursor_source = cursor_source
        self.cursor_revision = revision
