# 来源契约

来源 POST /exports 接受 campaign_ids 与 period，返回 job_id、request_digest；GET 状态包含 state、revision、download_url、expires_at。完成文件是 JSONL，每行 campaign_id、period、proof_id。回调可重送和乱序；签名 URL 仅作临时传输资格。

已有单响应输入为 fixtures/sample.json，含 source_id、url、status、headers 和 body_base64；原件摘要由 source.Response.sha256 给出。extract.extract 目前只读取 records 列表中的 job_id，不能把空列表解释为完整采集。ledger.receipt 把单次响应摘要和提取条目关联，尚无跨请求的完成资格或恢复状态。

来源配置与真实凭据由运行时提供，样例为合成数据。
