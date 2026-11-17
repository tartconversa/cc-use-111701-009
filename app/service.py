"""既有单响应采集入口。"""
import json
import sys
from pathlib import Path
from .source import from_packet,fetch
from .extract import extract
from .ledger import receipt

KEY='job_id'

def inspect(packet):
    response=from_packet(packet)
    return receipt(response,extract(response,KEY))

def collect_one(url,source_id):
    response=fetch(url,source_id)
    return receipt(response,extract(response,KEY))

def main():
    if len(sys.argv)!=2: raise SystemExit('请提供来源响应文件')
    print(json.dumps(inspect(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
