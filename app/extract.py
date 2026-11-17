"""当前单个 JSON 响应的条目提取。"""
import json

def extract(response, key):
    if response.status != 200: raise ValueError('来源未返回成功实体')
    value=json.loads(response.body)
    if not isinstance(value,dict) or not isinstance(value.get('records'),list):
        raise ValueError('缺少 records 数组')
    found=[]
    for row in value['records']:
        if not isinstance(row,dict) or not isinstance(row.get(key),str) or not row[key]:
            raise ValueError('条目身份无效')
        found.append({'key':row[key], 'value':row})
    return found
