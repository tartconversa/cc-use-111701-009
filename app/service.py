"""采集入口：单响应收据与修订流交付。"""
import json
import sys
from pathlib import Path

from .source import fetch, from_packet
from .extract import extract
from .ledger import receipt
from .sheet import Ledger, loads_strict
from .ingest import apply_response
from .delivery import build_manifest

KEY='row_id'


def inspect(packet):
    response=from_packet(packet)
    return receipt(response,extract(response,KEY))


def collect_one(url,source_id):
    response=fetch(url,source_id)
    return receipt(response,extract(response,KEY))


def replay(packets, ledger=None):
    """按给定顺序应用多份来源响应，返回台账。"""
    ledger = ledger or Ledger()
    for packet in packets:
        response = from_packet(packet)
        apply_response(ledger, response)
    return ledger


def deliver(packets, ledger=None):
    """应用修订流并生成带版本归属的交付清单。"""
    ledger = replay(packets, ledger)
    return build_manifest(ledger)


def _json(response):
    return loads_strict(response.body.decode('utf-8'))


def collect_stream(url, source_id, *, headers=None, ledger=None, max_pages=100,
                   urls=None, fetch_fn=fetch):
    """从一个来源连续领取变更，直到来源不再给出新游标。

    ``urls`` 可以是按页给出的完整 URL 列表；默认跟随响应中的
    ``next_url``，否则在本地址上附加 ``cursor`` 查询参数。
    调用者负责在游标过期时先取整表快照再继续。

    同一页被来源重复返回（相同摘要）不会重复追加证据，循环在
    游标不再前进时结束，避免无界翻页。
    """
    ledger = ledger or Ledger()
    pending = list(urls) if urls else [url]
    seen_urls = set()
    seen_digests = set()
    pages = 0
    while pending:
        if pages >= max_pages:
            raise ValueError('分页超出读取预算')
        current = pending.pop(0)
        if current in seen_urls:
            raise ValueError('来源重复给出同一页')
        seen_urls.add(current)
        response = fetch_fn(current, source_id, headers=headers)
        if response.sha256 in seen_digests:
            # 换了游标却拿到完全相同的一页：来源没有前进，停止而不是空转
            raise ValueError('来源重复给出同一页')
        seen_digests.add(response.sha256)
        body = _json(response)
        apply_response(ledger, response, body)
        pages += 1
        payload = body.get('sheet', body)
        next_url = payload.get('next_url')
        cursor = payload.get('next_cursor')
        if next_url:
            pending.append(next_url)
        elif cursor is not None and payload.get('kind') in ('changes', 'view'):
            if cursor == (body.get('cursor') if isinstance(body, dict) else None):
                continue
            following = _with_cursor(current, cursor)
            if following not in seen_urls:
                pending.append(following)
    return ledger


def _with_cursor(url, cursor):
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['cursor'] = str(cursor)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def load_packets(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get('packets'), list):
        return value['packets']
    return [value]


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == '--stream':
        packets = load_packets(args[1])
        print(json.dumps(deliver(packets), ensure_ascii=False, indent=2))
        return
    if len(args) == 3 and args[0] == '--stream-into':
        # 先恢复已采台账，再接入新的修订，输出更新后的台账状态
        state = json.loads(Path(args[1]).read_text(encoding='utf-8'))
        ledger = Ledger.load(state)
        replay(load_packets(args[2]), ledger)
        sys.stdout.write(json.dumps(ledger.as_dict(), ensure_ascii=False,
                                    indent=2, sort_keys=True) + '\n')
        return
    if args and not args[0].startswith('-'):
        for path in args:
            print(json.dumps(inspect(json.loads(
                Path(path).read_text(encoding='utf-8'))), ensure_ascii=False, indent=2))
        return
    raise SystemExit('请提供来源响应文件，或用 --stream <文件> 交付修订流')


if __name__=='__main__': main()
