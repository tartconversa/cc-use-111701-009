# 协作费用表的行身份与修订流采集

当前程序在既有单响应收据之外，接入表格来源的修订流：按稳定行 ID、
列 ID 和表格版本领取活动费用及其凭证链接，并把“同版本字段”和
“待补领字段”分开交付。

模块分工：

* `source.py` —— 保存响应状态、头和原始字节；
* `extract.py` —— 旧流程的单响应 `records` 提取；
* `ledger.py` —— 把来源与响应摘要绑成单响应收据；
* `sheet.py` —— 追加式修订流台账：行/列/版本身份、游标衔接、幂等恢复；
* `ingest.py` —— 把 `changes` / `view` / `snapshot` / `cursor_expired`
  响应应用到台账，原子且可重放；
* `delivery.py` —— 生成带版本归属的交付清单；
* `service.py` —— 单响应与修订流入口。

来源与字段约定见 docs/source-contract.md。

运行既有示例：

    python -m app.service fixtures/sample.json

运行修订流交付：

    python -m app.service --stream fixtures/revision_stream.json

在已采台账上接入新响应（幂等重放、跨运行恢复）：

    python -m app.service --stream-into state.json fixtures/revision_stream.json

运行测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests
