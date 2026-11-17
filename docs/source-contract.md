# 来源契约

协作表 API 返回 sheet_id、revision、rows[{row_id,cells:{column_id:value}}] 和变更游标；变更事件有 revision、row_id、column_id、operation。表格列 ID 稳定，显示名称及位置可变；视图过滤单独配置。游标过期时可取得带 revision 的全表快照。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 row_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
