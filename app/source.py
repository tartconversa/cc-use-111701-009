"""单次来源响应读取与身份校验。"""
import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

@dataclass(frozen=True)
class Response:
    source_id: str
    url: str
    status: int
    headers: tuple[tuple[str,str], ...]
    body: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.body).hexdigest()

    def header(self, name):
        return next((v for k,v in self.headers if k.lower()==name.lower()), None)

def from_packet(packet):
    if not isinstance(packet.get('source_id'),str) or not packet['source_id']:
        raise ValueError('来源身份无效')
    status=packet.get('status')
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError('响应状态无效')
    headers=packet.get('headers')
    if not isinstance(headers,list) or any(not isinstance(x,list) or len(x)!=2 or not all(isinstance(y,str) for y in x) for x in headers):
        raise ValueError('响应头无效')
    try: body=base64.b64decode(packet['body_base64'],validate=True)
    except Exception as exc: raise ValueError('实体编码无效') from exc
    return Response(packet['source_id'],packet['url'],status,tuple(map(tuple,headers)),body)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def fetch(url,source_id,*,headers=None,max_bytes=1048576):
    if type(max_bytes) is not int or max_bytes < 0: raise ValueError('读取预算无效')
    request=urllib.request.Request(url,headers=headers or {})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try: stream=opener.open(request,timeout=5)
    except urllib.error.HTTPError as exc: stream=exc
    with stream:
        body=stream.read(max_bytes+1)
        if len(body)>max_bytes: raise ValueError('响应超过读取预算')
        return Response(source_id,stream.geturl(),stream.code,tuple(stream.headers.raw_items()),body)
