import json
import unittest
from pathlib import Path
from app.service import inspect
from app.source import from_packet

PACKET=json.loads((Path(__file__).resolve().parents[1]/'fixtures'/'sample.json').read_text(encoding='utf-8'))

class ExistingFlowTest(unittest.TestCase):
    def test_single_response_receipt(self):
        result=inspect(PACKET)
        self.assertEqual(result['source_id'],'source-a')
        self.assertEqual(result['items'][0]['key'],'job-1')
        self.assertEqual(len(result['response_sha256']),64)

    def test_bad_encoding_is_rejected(self):
        with self.assertRaises(ValueError): from_packet({**PACKET,'body_base64':'?'})

    def test_missing_identity_is_rejected(self):
        with self.assertRaises(ValueError): inspect({**PACKET,'source_id':''})

if __name__=='__main__': unittest.main()
