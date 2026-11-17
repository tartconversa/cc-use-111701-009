# 来源契约

## 稳定身份

协作表 API 以三组稳定身份描述数据，台账只按它们定位与合并：

| 身份 | 字段 | 不变量 |
| --- | --- | --- |
| 行 | `row_id` | 行排序、筛选视图不改变 |
| 列 | `column_id` | 显示名称与列位置可任意变化 |
| 版本 | `revision` | 表格版本，随每次变更递增 |

响应中的 `name`（显示列名）、`position`（列位置）和行在数组中的顺序
仅作为元数据随附，**不参与取值、合并、缺失或删除判断**。

## 响应类型（`kind`）

| kind | 含义 | 对台账的影响 |
| --- | --- | --- |
| `changes` | 增量变更事件 | 按事件追加观察，推进游标 |
| `view` | 筛选视图的一页 | 只补充可见证据；**隐藏行不是删除** |
| `cursor_expired` | 游标过期信号 | 只记录衔接依据，不推进已采修订 |
| `snapshot` | 整表快照 | 重新对齐；保留并衔接此前已采证据 |

变更事件为 `{revision,row_id,column_id,operation,value}`，`operation` 取
`set`、`clear`、`delete`、`approve`、`revoke_approval`。一个响应可带回
多个修订的事件，但事件版本不得晚于响应版本。

## 历史不可抹去

* 删除（`delete`）与撤销审核（`revoke_approval`）是**追加**的历史事实，
  更晚的修订不会覆盖它们；历史观察完整保存在交付清单的 `history` 中。
* 更正后的金额来自更晚修订，旧金额仍然留在历史里。
* 快照缺席只表示“该版本的全表里没有这一行”，记为
  `absent_from_snapshot`，与真正的删除分开报告。
* `delete` 之后若更晚的快照重新包含该行，行恢复为活动并记录
  `restore_from_snapshot`；先前的删除证据仍在历史中。

## 游标与整表重取

响应可携带 `cursor` / `next_cursor`；游标过期时来源返回
`cursor_expired`（可带表格当前 `revision`），随后应取 `snapshot`。

台账在 `checkpoints` 中按顺序保存每个游标与修订的衔接点。快照的
`checkpoint.snapshot_of` 记录对齐前已采到的：

* `previous_head_revision` —— 已采行的最高版本；
* `previous_head_evidence` —— 该版本对应的来源摘要；
* `previous_cursor` / `previous_cursor_source` —— 对齐前使用的游标；
* `previous_evidence` —— 对齐前已登记的全部来源摘要。

**已采修订优先**：低于已采修订的快照会被拒绝；快照中已有的更晚修订
不会被旧快照回退（记为 `snapshot_stale`）。

## 幂等与原子性

* 同一响应（`sha256` 相同）重复输入是幂等的，不会重复追加历史。
* 一次响应要么完整应用，要么完全不改变台账；某条事件无效时整页
  不留下半套证据，也不登记摘要，可以修正后重新领取。
* 同一版本出现冲突值、同一请求内重复声明同一变更、版本回退、
  同一表格身份不符，均被拒绝。

## 字段版本与待补领

金额、审核状态、凭证可能在不同修订中先后到达。交付清单不会把它们
拼成“行号 + 显示列名”的一条记录，而是：

* `groups.{amount,approval,voucher}` 各自给出 `column_id`、值、`revision`
  和来源摘要；
* `cohorts` 按修订聚拢**确实同版本**的字段组；
* `coherent` 表示所有已到达的业务字段组同版本；
* `pending` 列出缺失（`missing:*`）或跨修订（`mixed:*`）的组，
  供业务侧继续补领；
* `deleted` / `absent` 单列已删除与快照缺席的行，附最后已知单元格与版本。

## 已有单响应输入

`fixtures/sample.json` 含 `source_id`、`url`、`status`、`headers` 和
`body_base64`；原件摘要由 `source.Response.sha256` 给出。`extract.extract`
仍用于旧流程的 `records` 提取，`ledger.receipt` 把单次响应摘要和提取条目
关联。修订流见 `fixtures/revision_stream.json`。

来源配置与真实凭据由运行时提供，样例为合成数据。
