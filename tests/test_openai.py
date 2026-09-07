import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from productgen.ai import OpenAI, VISUAL_SCHEMA, generation_schema


class OpenAIAdapterTests(unittest.TestCase):
    def provider(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'test-key-only','OPENAI_MODEL':'gpt-4.1-mini','OPENAI_VISION_MODEL':'gpt-4.1-mini'}, clear=True):
            return OpenAI()

    def test_responses_image_schema_and_output_parsing(self):
        captured=[]
        def respond(request, timeout):
            captured.append(request)
            return io.BytesIO(json.dumps({'status':'completed','output':[{'type':'reasoning','summary':[]},{'type':'message','content':[{'type':'output_text','text':'{"facts":[],"uncertainties":[]}'}]}]}).encode())
        with patch('urllib.request.urlopen', side_effect=respond):
            result=self.provider().complete('Describe image as JSON', [{'mime':'image/png','data':'test-data'}], VISUAL_SCHEMA)
        self.assertEqual(result, {'facts':[],'uncertainties':[]})
        request=captured[0]
        self.assertEqual(request.full_url,'https://api.openai.com/v1/responses')
        self.assertNotIn('test-key-only', request.full_url)
        self.assertEqual(request.get_header('Authorization'),'Bearer test-key-only')
        body=json.loads(request.data)
        self.assertFalse(body['store'])
        self.assertTrue(body['text']['format']['strict'])
        self.assertEqual(body['input'][0]['content'][1]['image_url'],'data:image/png;base64,test-data')

    def test_refusal_and_incomplete_not_accepted(self):
        for envelope in [{'status':'incomplete','output':[]},{'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'No'}]}]},{'status':'completed','output':[]}]:
            with self.subTest(envelope=envelope), patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(envelope).encode())), self.assertRaises(ValueError):
                self.provider().complete('JSON',schema=VISUAL_SCHEMA)

    def test_quota_retries_then_fails_without_exposing_key(self):
        error=urllib.error.HTTPError('https://api.openai.com/v1/responses',429,'Quota',{},None)
        with patch('urllib.request.urlopen',side_effect=error) as request, patch('time.sleep'), self.assertRaises(ValueError) as result:
            self.provider().complete('JSON',schema=VISUAL_SCHEMA)
        self.assertEqual(request.call_count,3)
        self.assertNotIn('test-key-only',str(result.exception))

    def test_structured_schema_tracks_selected_platforms(self):
        schema=generation_schema(['Artikel::Farbe'],['amazon','shopify'])
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['attributes']['items']['properties']['id']['enum'],['Artikel::Farbe'])
        self.assertEqual(set(schema['properties']['descriptions']['required']),{'amazon','shopify'})
        self.assertNotIn('otto',schema['properties']['descriptions']['properties'])

    def test_missing_key_has_actionable_error(self):
        with patch.dict(os.environ,{},clear=True), self.assertRaisesRegex(ValueError,'OPENAI_API_KEY'):
            OpenAI()


if __name__=='__main__':
    unittest.main()
