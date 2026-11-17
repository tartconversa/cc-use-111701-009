"""将当前响应条目连同原件身份交付给调用者。"""
import json
from pathlib import Path

def receipt(response, items):
    return {'source_id':response.source_id,'url':response.url,
            'response_sha256':response.sha256,'items':items}

def save(path, result):
    Path(path).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
