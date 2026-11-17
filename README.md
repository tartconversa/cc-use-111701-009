# 协作费用表的行身份与修订流采集

当前程序只接收一份来源 JSON 响应并生成条目收据。source.py 保存响应状态、头和原始字节；extract.py 读取现有 records 条目；ledger.py 绑定来源与响应摘要；service.py 提供单响应入口。项目使用 Python 标准库，不接触真实企业系统。现有单响应流程不证明跨页、跨资源或跨版本采集完整。

来源与字段约定见 docs/source-contract.md。

运行当前示例：

    python -m app.service fixtures/sample.json

运行测试：

    python -m unittest discover -s tests -v

检查编译：

    python -m compileall -q app tests
