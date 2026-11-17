"""修订流采集的行为测试。"""
import base64
import json
import unittest
from pathlib import Path

from app.delivery import build_manifest
from app.ingest import apply_response
from app.service import deliver, replay
from app.sheet import Ledger
from app.source import from_packet

ROOT = Path(__file__).resolve().parents[1]
STREAM = json.loads((ROOT / 'fixtures' / 'revision_stream.json').read_text(encoding='utf-8'))
PACKET = json.loads((ROOT / 'fixtures' / 'sample.json').read_text(encoding='utf-8'))


def make_packet(body, source_id='source-a', url='https://example.test/sheet'):
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
    return {
        'source_id': source_id,
        'url': url,
        'status': 200,
        'headers': [['Content-Type', 'application/json']],
        'body_base64': base64.b64encode(raw).decode('ascii'),
    }


def make_response(body, **kwargs):
    return from_packet(make_packet(body, **kwargs))


def ledger_of(*packets):
    ledger = Ledger()
    for packet in packets:
        apply_response(ledger, from_packet(packet))
    return ledger


def by_row(manifest):
    return {row['row_id']: row for row in manifest['rows']}


class StreamFlowTest(unittest.TestCase):
    def setUp(self):
        self.manifest = deliver(STREAM)
        self.rows = by_row(self.manifest)

    def test_head_and_cursor_come_from_last_checkpoint(self):
        self.assertEqual(self.manifest['head_revision'], 15)
        self.assertEqual(self.manifest['cursor'], 'cur-15')
        kinds = [item['kind'] for item in self.manifest['checkpoints']]
        self.assertIn('snapshot', kinds)
        self.assertIn('cursor_expired', kinds)

    def test_amount_correction_keeps_later_value_and_history(self):
        amount = self.rows['row-1']['groups']['amount']['columns']['amount']
        self.assertEqual(amount['value'], '12000.00')
        self.assertEqual(amount['revision'], 15)
        original = [
            item for item in self.manifest['history']
            if item['row_id'] == 'row-1'
            and item['column_id'] == 'amount'
            and item['value'] == '12500.00'
        ]
        self.assertTrue(original, '被更正的旧金额必须留在历史里')

    def test_revoked_approval_is_not_erased(self):
        row = self.rows['row-1']
        self.assertEqual(row['approval_state']['status'], 'revoked')
        self.assertEqual(row['approval_state']['revision'], 11)
        approved = [item for item in self.manifest['history']
                    if item['operation'] == 'revoke_approval']
        self.assertEqual(len(approved), 1)

    def test_filtered_view_does_not_delete_hidden_row(self):
        self.assertNotIn('row-2', self.rows)
        absent = {item['row_id'] for item in self.manifest['absent']}
        deleted = {item['row_id'] for item in self.manifest['deleted']}
        # row-2 只是未出现在视图/快照中，从未被真正删除
        self.assertIn('row-2', absent)
        self.assertNotIn('row-2', deleted)

    def test_explicit_delete_is_reported_separately(self):
        deleted = {item['row_id']: item for item in self.manifest['deleted']}
        self.assertIn('row-3', deleted)
        self.assertEqual(deleted['row-3']['status_revision'], 13)

    def test_columns_identified_by_id_not_display_name(self):
        self.assertEqual(self.manifest['columns']['amount']['name'], '金额')
        # 显示顺序被打乱（approval 在位置 0），取值仍按 column_id
        self.assertEqual(self.rows['row-1']['groups']['approval']['revision'], 15)

    def test_same_revision_flag(self):
        self.assertTrue(self.rows['row-4']['same_revision'])
        # row-1 的业务字段来自快照 r15，但审核动作来自 r11
        self.assertFalse(self.rows['row-1']['same_revision'])
        self.assertEqual(self.rows['row-1']['revisions'], [11, 15])


class VersionCoherenceTest(unittest.TestCase):
    def test_amount_and_voucher_arriving_in_different_revisions_is_pending(self):
        ledger = ledger_of(
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 1,
                'changes': [
                    {'revision': 1, 'row_id': 'r1',
                     'column_id': 'amount', 'operation': 'set', 'value': '10.00'},
                ],
            }),
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 2,
                'changes': [
                    {'revision': 2, 'row_id': 'r1',
                     'column_id': 'voucher_url', 'operation': 'set',
                     'value': 'https://v/v-1'},
                ],
            }),
        )
        manifest = build_manifest(ledger)
        row = by_row(manifest)['r1']
        self.assertEqual(
            row['groups']['amount']['columns']['amount']['revision'], 1)
        self.assertEqual(
            row['groups']['voucher']['columns']['voucher_url']['revision'], 2)
        self.assertFalse(row['coherent'])
        pending = {item['row_id']: item for item in manifest['pending']}
        self.assertIn('r1', pending)
        self.assertIn('approval', pending['r1']['missing_groups'])
        self.assertIn('mixed:amount', pending['r1']['reason'])

    def test_missing_group_is_reported_pending(self):
        ledger = ledger_of(make_packet({
            'sheet_id': 's', 'kind': 'changes', 'revision': 5,
            'changes': [
                {'revision': 5, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
            ],
        }))
        pending = build_manifest(ledger)['pending']
        self.assertEqual(pending[0]['row_id'], 'r1')
        self.assertEqual(pending[0]['missing_groups'], ['approval', 'voucher'])


class CursorAndSnapshotTest(unittest.TestCase):
    def base_ledger(self):
        return ledger_of(make_packet({
            'sheet_id': 's', 'kind': 'changes', 'revision': 4,
            'cursor': 'c4',
            'changes': [
                {'revision': 4, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
            ],
        }))

    def test_snapshot_after_expiry_links_old_evidence(self):
        ledger = self.base_ledger()
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'cursor_expired', 'revision': 5,
            'cursor': 'c4', 'reason': 'too_old',
        }))
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'snapshot', 'revision': 6,
            'cursor': 'c6', 'reason': 'cursor_expired',
            'columns': [{'column_id': 'amount', 'name': '金额', 'position': 0}],
            'rows': [{'row_id': 'r1', 'cells': {'amount': '2'}},
                     {'row_id': 'r2', 'cells': {'amount': '3'}}],
        }))
        snapshot = [c for c in ledger.checkpoints if c['kind'] == 'snapshot'][-1]
        self.assertEqual(snapshot['snapshot_of']['previous_head_revision'], 4)
        self.assertEqual(snapshot['snapshot_of']['previous_cursor'], 'c4')
        self.assertTrue(snapshot['snapshot_of']['previous_evidence'])
        self.assertEqual(ledger.head_revision, 6)
        # 旧金额证据仍在历史中
        self.assertTrue([o for o in ledger.observations
                         if o.revision == 4 and o.value == '1'])

    def test_snapshot_older_than_a_collected_revision_is_rejected(self):
        ledger = self.base_ledger()
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'changes', 'revision': 9,
            'changes': [
                {'revision': 9, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '9'},
            ],
        }))
        with self.assertRaises(ValueError):
            apply_response(ledger, make_response({
                'sheet_id': 's', 'kind': 'snapshot', 'revision': 8,
                'rows': [{'row_id': 'r1', 'cells': {'amount': '8'}}],
            }))
        # 新修订的证据没有被旧快照覆盖
        self.assertEqual(ledger.rows['r1']['cells']['amount'], '9')

    def test_replaying_same_response_is_idempotent(self):
        packet = make_packet({
            'sheet_id': 's', 'kind': 'changes', 'revision': 3,
            'changes': [
                {'revision': 3, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
            ],
        })
        ledger = ledger_of(packet, packet)
        self.assertEqual(len(ledger.observations), 1)

    def test_lower_revision_is_rejected(self):
        ledger = self.base_ledger()
        with self.assertRaises(ValueError):
            apply_response(ledger, make_response({
                'sheet_id': 's', 'kind': 'changes', 'revision': 2,
                'changes': [],
            }))


class IdentityAndInputTest(unittest.TestCase):
    def test_row_order_and_display_names_do_not_identify_fields(self):
        ledger = ledger_of(make_packet({
            'sheet_id': 's', 'kind': 'snapshot', 'revision': 2,
            'columns': [
                {'column_id': 'amount', 'name': 'A', 'position': 0},
                {'column_id': 'voucher_url', 'name': 'B', 'position': 1},
            ],
            'rows': [
                {'row_id': 'r1', 'cells': {'amount': '1', 'voucher_url': 'u1'}},
            ],
        }))
        # 列被重命名、重排后，同一 column_id 仍然命中同一字段
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'snapshot', 'revision': 3,
            'columns': [
                {'column_id': 'voucher_url', 'name': '凭证', 'position': 0},
                {'column_id': 'amount', 'name': '金额', 'position': 1},
            ],
            'rows': [
                {'row_id': 'r1', 'cells': {'voucher_url': 'u2', 'amount': '2'}},
            ],
        }))
        self.assertEqual(ledger.rows['r1']['cells']['amount'], '2')
        self.assertEqual(ledger.rows['r1']['cells']['voucher_url'], 'u2')

    def test_sheet_mismatch_is_rejected(self):
        ledger = ledger_of(make_packet({
            'sheet_id': 's1', 'kind': 'changes', 'revision': 1, 'changes': [],
        }))
        with self.assertRaises(ValueError):
            apply_response(ledger, make_response({
                'sheet_id': 's2', 'kind': 'changes', 'revision': 2, 'changes': [],
            }))

    def test_duplicate_json_keys_are_rejected(self):
        text = (
            '{"sheet_id":"s","kind":"changes","revision":1,'
            '"changes":[{"revision":1,"row_id":"r1","column_id":"amount",'
            '"operation":"set","value":"1","value":"2"}]}'
        )
        packet = {
            'source_id': 'source-a',
            'url': 'https://example.test/sheet',
            'status': 200,
            'headers': [],
            'body_base64': base64.b64encode(text.encode('utf-8')).decode('ascii'),
        }
        with self.assertRaises(ValueError):
            apply_response(Ledger(), from_packet(packet))

    def test_conflicting_value_in_same_revision_is_rejected(self):
        body = {
            'sheet_id': 's', 'kind': 'changes', 'revision': 3,
            'changes': [
                {'revision': 3, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
            ],
        }
        ledger = ledger_of(make_packet(body))
        body2 = {
            'sheet_id': 's', 'kind': 'changes', 'revision': 3,
            'changes': [
                {'revision': 3, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '2'},
            ],
        }
        with self.assertRaises(ValueError):
            apply_response(ledger, make_response(body2))


class LedgerPersistenceTest(unittest.TestCase):
    def test_roundtrip_preserves_evidence_and_pending(self):
        ledger = replay(STREAM)
        restored = Ledger.load(json.loads(json.dumps(ledger.as_dict())))
        self.assertEqual(restored.head_revision, ledger.head_revision)
        self.assertEqual(restored.cursor, ledger.cursor)
        self.assertEqual(
            [item.as_dict() for item in restored.observations],
            [item.as_dict() for item in ledger.observations],
        )
        original = build_manifest(ledger)
        again = build_manifest(restored)
        self.assertEqual(original['rows'], again['rows'])
        self.assertEqual(original['pending'], again['pending'])
        self.assertEqual(original['absent'], again['absent'])

    def test_snapshot_checkpoint_structurally_plain(self):
        ledger = replay(STREAM)
        saved = ledger.as_dict()
        json.dumps(saved)  # 不含日期等不可序列化对象
        kinds = {item['kind'] for item in saved['checkpoints']}
        self.assertTrue({'snapshot', 'cursor_expired'} <= kinds)


class AtomicityTest(unittest.TestCase):
    def test_invalid_event_leaves_ledger_unchanged(self):
        body = {
            'sheet_id': 's', 'kind': 'changes', 'revision': 7,
            'changes': [
                {'revision': 7, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
                {'revision': 7, 'row_id': 'r2',
                 'column_id': 'amount', 'operation': 'explode'},
            ],
        }
        ledger = Ledger()
        before = json.dumps(ledger.as_dict(), sort_keys=True)
        with self.assertRaises(ValueError):
            apply_response(ledger, make_response(body))
        self.assertEqual(json.dumps(ledger.as_dict(), sort_keys=True), before)
        # 修正后同一摘要仍可重新领取（未被登记为已采）
        body['changes'][1]['operation'] = 'set'
        body['changes'][1]['value'] = '1'
        apply_response(ledger, make_response(body))
        self.assertEqual(ledger.rows['r2']['cells']['amount'], '1')


class PaginationTest(unittest.TestCase):
    def test_collect_stream_follows_cursor_and_stops(self):
        from app.service import collect_stream
        from app.source import Response

        pages = {
            'https://example.test/sheet': {
                'sheet_id': 's', 'kind': 'changes', 'revision': 1,
                'next_cursor': 'c2', 'changes': [
                    {'revision': 1, 'row_id': 'r1', 'column_id': 'amount',
                     'operation': 'set', 'value': '1'},
                ],
            },
        }
        pages['https://example.test/sheet?cursor=c2'] = {
            'sheet_id': 's', 'kind': 'changes', 'revision': 2,
            'changes': [
                {'revision': 2, 'row_id': 'r1', 'column_id': 'voucher_url',
                 'operation': 'set', 'value': 'u'},
            ],
        }
        calls = []

        def fake_fetch(url, source_id, headers=None, max_bytes=1048576):
            calls.append(url)
            raw = json.dumps(pages[url]).encode('utf-8')
            return Response(source_id, url, 200, (), raw)

        ledger = collect_stream('https://example.test/sheet', 's',
                                fetch_fn=fake_fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(ledger.head_revision, 2)
        self.assertEqual(ledger.rows['r1']['cells']['voucher_url'], 'u')

    def test_repeated_page_is_rejected(self):
        from app.service import collect_stream
        from app.source import Response

        body = {
            'sheet_id': 's', 'kind': 'changes', 'revision': 1,
            'next_cursor': 'c1', 'changes': [],
        }
        raw = json.dumps(body).encode('utf-8')
        same = Response('s', 'https://example.test/sheet', 200, (), raw)

        def fake_fetch(url, source_id, headers=None, max_bytes=1048576):
            return same

        with self.assertRaises(ValueError):
            collect_stream('https://example.test/sheet', 's',
                           fetch_fn=fake_fetch, max_pages=5)


class SnapshotSemanticsTest(unittest.TestCase):
    def test_later_snapshot_restores_deleted_row_without_erasing_history(self):
        ledger = ledger_of(
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 1,
                'changes': [
                    {'revision': 1, 'row_id': 'r1',
                     'column_id': 'amount', 'operation': 'set', 'value': '5'},
                ],
            }),
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 2,
                'changes': [
                    {'revision': 2, 'row_id': 'r1', 'operation': 'delete'},
                ],
            }),
            make_packet({
                'sheet_id': 's', 'kind': 'snapshot', 'revision': 3,
                'rows': [{'row_id': 'r1', 'cells': {'amount': '5'}}],
            }),
        )
        manifest = build_manifest(ledger)
        self.assertEqual(manifest['deleted'], [])
        self.assertEqual([r['row_id'] for r in manifest['rows']], ['r1'])
        deletes = [o for o in manifest['history'] if o['operation'] == 'delete']
        self.assertEqual(len(deletes), 1, '删除观察不能被恢复抹去')
        restores = [o for o in manifest['history']
                    if o['operation'] == 'restore_from_snapshot']
        self.assertEqual(len(restores), 1)

    def test_empty_snapshot_still_advances_head(self):
        ledger = ledger_of(make_packet({
            'sheet_id': 's', 'kind': 'changes', 'revision': 1,
            'cursor': 'c1',
            'changes': [
                {'revision': 1, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
            ],
        }))
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'snapshot', 'revision': 4,
            'cursor': 'c4', 'columns': [{'column_id': 'amount'}], 'rows': [],
        }))
        self.assertEqual(ledger.head_revision, 4)
        self.assertEqual(ledger.rows['r1']['status'], 'absent_from_snapshot')

    def test_snapshot_with_partial_columns_keeps_other_cells(self):
        ledger = ledger_of(make_packet({
            'sheet_id': 's', 'kind': 'changes', 'revision': 1,
            'cursor': 'c1',
            'changes': [
                {'revision': 1, 'row_id': 'r1',
                 'column_id': 'amount', 'operation': 'set', 'value': '1'},
                {'revision': 1, 'row_id': 'r1',
                 'column_id': 'voucher_url', 'operation': 'set', 'value': 'u'},
            ],
        }))
        apply_response(ledger, make_response({
            'sheet_id': 's', 'kind': 'snapshot', 'revision': 2,
            'columns': [{'column_id': 'amount'}, {'column_id': 'voucher_url'}],
            'rows': [{'row_id': 'r1', 'cells': {'amount': '2'}}],
        }))
        self.assertEqual(ledger.rows['r1']['cells']['amount'], '2')
        self.assertEqual(ledger.rows['r1']['cells']['voucher_url'], 'u')


class AuditingLastWeekTest(unittest.TestCase):
    def test_missing_expense_can_be_explained_from_history(self):
        """上周看到的费用这周不在表里：区分删除、视图隐藏、快照缺席。"""
        ledger = ledger_of(
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 20,
                'changes': [
                    {'revision': 20, 'row_id': 'expense-9',
                     'column_id': 'amount', 'operation': 'set',
                     'value': '660.00'},
                    {'revision': 20, 'row_id': 'expense-9',
                     'column_id': 'voucher_url', 'operation': 'set',
                     'value': 'https://vault.test/v-9'},
                    {'revision': 20, 'row_id': 'expense-9',
                     'column_id': 'approval_status', 'operation': 'set',
                     'value': 'approved'},
                ],
            }),
            # 审核后更正金额
            make_packet({
                'sheet_id': 's', 'kind': 'changes', 'revision': 21,
                'changes': [
                    {'revision': 21, 'row_id': 'expense-9',
                     'column_id': 'amount', 'operation': 'set',
                     'value': '600.00'},
                ],
            }),
            # 视图隐藏了它：不是删除
            make_packet({
                'sheet_id': 's', 'kind': 'view', 'revision': 22,
                'filter_id': 'view-q4', 'rows': [],
            }),
        )
        manifest = build_manifest(ledger)
        row = by_row(manifest)['expense-9']
        # 视图未见，但行仍活动，且仍可交付
        self.assertNotIn('expense-9',
                         [item['row_id'] for item in manifest['deleted']])
        self.assertEqual(row['groups']['amount']['columns']['amount']['value'],
                         '600.00')
        # 审核状态列与审核动作各自带版本，说明谁属于哪个版本
        self.assertEqual(row['cohorts'][0]['revision'], 20)
        self.assertEqual(row['cohorts'][0]['groups'],
                         ['approval', 'voucher'])
        self.assertEqual(row['cohorts'][1]['revision'], 21)
        self.assertEqual(row['cohorts'][1]['groups'], ['amount'])
        self.assertFalse(row['same_revision'])
        pending = {item['row_id']: item for item in manifest['pending']}
        self.assertEqual(pending['expense-9']['split_groups'], ['approval', 'voucher'])


class LegacyFlowStillWorksTest(unittest.TestCase):
    def test_single_response_receipt_unchanged(self):
        from app.service import inspect
        result = inspect(PACKET)
        self.assertEqual(result['items'][0]['key'], 'row-1')


if __name__ == '__main__':
    unittest.main()
