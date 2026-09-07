import base64
import copy
import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from productgen.ai import empty_draft, generate, validate_generation, validate_visual
from productgen.catalog import ATTRIBUTE_COLUMNS, default_rules, description_rules, ensure_export_schema, read_csv, seed_catalog
from productgen.core import build_export, compose, csv_bytes, refresh_derived, resolve_paths, validate_config, validate_draft, validate_product, value_errors
from productgen.storage import Store
from server import Application, ROOT, ThreadingHTTPServer, handler_for

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='


def product(**overrides):
    return {"category": "seidentuch-damen", "title": "Seidentuch Blumenmuster", "price": "39,90", "weight": "35", "material": "100% Seide", "size": "53 × 53", "sku": "TEST-001", "platforms": ["shopify"], "attribute_languages": ["de"], "description_languages": ["de"], "images": [{"name": "fixture.png", "mime": "image/png", "data": PNG}], **overrides}


def review_payload(d):
    return {"revision": d["revision"], "attributes": {lang: {r["id"]: r["value"] for r in rows} for lang, rows in d["attributes"].items()}, "descriptions": copy.deepcopy(d["descriptions"])}


class FakeAI:
    model = 'test-provider'

    def __init__(self):
        self.calls = []

    def complete(self, prompt, images=(), schema=None):
        self.calls.append((prompt, images))
        if images:
            return {"facts": ["Blaues Blumenmuster"], "uncertainties": []}
        data = json.loads(prompt.split('INPUT DATA:\n')[1])
        attributes = []
        for r in data['requested_attributes']:
            attributes.append({"id": r['id'], "value": r['allowed_values'][0] if r['allowed_values'] else 'Blau', "confidence": 0.8, "warnings": []})
        return {"attributes": attributes, "master": "Factual product content", "descriptions": {pid: {"title": "Seidentuch mit Blumenmuster", "description": "Ein Seidentuch aus 100% Seide in der Größe 53 × 53 cm.", "bullets": ['Factual detail'] * r['bullet_count'], "meta_description": "Seidentuch aus Seide" if pid == 'shopify' else '', "backend_keywords": 'seidentuch blumenmuster seide' if pid == 'amazon' else ''} for pid, r in data['platforms'].items()}}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config(seed_catalog(ROOT / 'BackgroundInformation'))

    def draft(self, **overrides):
        return empty_draft(validate_product(product(**overrides), self.config), self.config)

    def test_source_counts_and_normalization(self):
        self.assertEqual(len(self.config['categories']), 6)
        self.assertEqual(len(self.config['category_tables']), 13)
        rules = next(s for s in self.config['sets'] if s['id'] == 'ai_attributes')['rules']
        self.assertEqual(len(rules), 22)
        self.assertEqual(sum(r['name'] == 'Besondere Merkmale' for r in rules), 1)
        self.assertTrue(all(r['name'] == r['name'].strip() and r['group'] == r['group'].strip() for r in rules))

    def test_multiline_csv_quoting(self):
        rows = read_csv('attribute_group;attribute_name;attribute_value\nArtikel;Text;"First; line\nSecond ""quoted"" line"\n')
        self.assertEqual(rows[0]['attribute_value'], 'First; line\nSecond "quoted" line')
        with self.assertRaises(ValueError):
            default_rules([{'incorrect': 'header'}])

    def test_platform_filter_and_activation(self):
        d = self.draft()
        rows = d['attributes']['de']
        self.assertFalse(any(r['rule']['platform'] == 'amazon' and r['rule']['export'] and not r['rule']['name'].startswith('Verkaufskanal [') for r in rows))
        flags = [r for r in rows if r['rule']['name'].startswith('Verkaufskanal [')]
        self.assertEqual(len(flags), 5)
        self.assertTrue(all(r['value'] == ('Y' if r['rule']['platform'] == 'shopify' else 'N') for r in flags))
        self.assertEqual(set(d['descriptions']['de']), {'shopify'})

    def test_hidden_dependencies_preserved(self):
        d = self.draft(platforms=['amazon'])
        optic = next(r for r in d['attributes']['de'] if r['id'] == 'unicorn2-OttoMarket::Optik_ai')
        self.assertFalse(optic['rule']['export'])
        optic['value'] = 'Floral'
        refresh_derived(d)
        self.assertEqual(next(r['value'] for r in d['attributes']['de'] if r['id'] == 'AmazonLister::Muster'), 'Floral')
        self.assertFalse(any(p['platform'] == 'shopify' for p in d['category_paths']))

    def test_category_filters_and_no_duplicates(self):
        for category in self.config['categories']:
            rows = compose(self.config, product(category=category['id'], platforms=[p['id'] for p in self.config['platforms']]))
            self.assertEqual(len(rows), len({r['id'] for r in rows}))
            if category['id'] != 'kette':
                self.assertFalse(any(r['name'] == 'Kettenart' for r in rows))

    def test_route_size_material_and_unmatched(self):
        cases = [({'size': '64 x 64'}, 'KategorienSeide65'), ({'size': '180 x 5'}, 'KategorienSeideBandeau'), ({'material': 'Seide / Baumwolle'}, 'KategorienSeideBaumwolle'), ({'category': 'seidentuch-herren'}, 'KategorienSeide53Herren')]
        for inputs, expected in cases:
            self.assertEqual(self.draft(**inputs)['category_table'], expected)
        with self.assertRaisesRegex(ValueError, 'No matching'):
            self.draft(size='99 x 99')
        self.assertEqual(self.draft(size='99 x 99', category_table='KategorienSeide86')['category_table'], 'KategorienSeide86')
        self.assertEqual(self.draft(category='kette', material='Baumwolle')['category_table'], 'KategorienKette')

    def test_invalid_input_and_images(self):
        cases = [dict(price='NaN'), dict(weight='-1'), dict(images=[]), dict(images=product()['images'] * 4), dict(ean='12345678'), dict(platforms=['missing']), dict(attribute_languages=['xx']), dict(images=[{'name':'x','mime':'image/png','data':base64.b64encode(b'not an image').decode()}])]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.draft(**fields)

    def test_constraint_validation(self):
        r = self.config['sets'][0]['rules'][0]
        r.update(required=True, max_length=4, allowed_values=['Blau'])
        self.assertFalse(value_errors(r, 'Blau'))
        self.assertTrue(value_errors(r, 'Länger'))
        self.assertTrue(value_errors(r, 'Rot'))
        self.assertTrue(value_errors(r, ''))
        r.update(format='number', required=False, allowed_values=[], max_length=None)
        self.assertFalse(value_errors(r, '1,25'))
        self.assertTrue(value_errors(r, 'NaN'))
        r['format']='boolean'
        self.assertTrue(value_errors(r, 'yes'))
        self.assertFalse(value_errors(r, False))

    def test_cyclic_and_missing_bindings_rejected(self):
        derived = next(s for s in self.config['sets'] if s['id']=='derived')['rules']
        derived[0]['binding'] = 'attribute.' + derived[0]['id']
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            validate_config(self.config)

    def test_ai_schema_rejects_extra_omitted_and_duplicate(self):
        result = {'attributes': [], 'master': '', 'descriptions': {}}
        with self.assertRaises(ValueError):
            validate_generation(result, ['required'], [])
        result['surprise'] = True
        with self.assertRaises(ValueError):
            validate_generation(result, [], [])
        with self.assertRaises(ValueError):
            validate_visual({'facts': 'bad', 'uncertainties': []})

    def test_language_generation_and_image_batch(self):
        ai = FakeAI()
        d = generate(self.draft(attribute_languages=['de','pl'], description_languages=['en','fr'], images=product()['images'] * 3), ai)
        self.assertEqual(len(ai.calls), 5)
        self.assertEqual(len(ai.calls[0][1]), 3)
        self.assertEqual(set(d['attributes']), {'de','pl'})
        self.assertEqual(set(d['descriptions']), {'en','fr'})
        for lang in d['attributes']:
            code = next(r for r in d['attributes'][lang] if r['id']=='Shopify-Attribute::product_type')
            self.assertEqual(code['value'], 'SILK')

    def test_unknown_values_do_not_create_facts(self):
        d = self.draft()
        color=next(r for r in d['attributes']['de'] if r['id']=='Artikel::Farbe')
        self.assertEqual(color['value'], '')
        self.assertTrue(any(i['severity']=='warning' and 'Farbe' in i['location'] for i in d['issues']))
        self.assertTrue(any(i['severity']=='error' and 'title' in i['location'] for i in d['issues']))

    def test_export_requires_approval_and_constraints(self):
        d = generate(self.draft(), FakeAI())
        with self.assertRaises(ValueError):
            build_export(d, self.config['export'])

    def test_html_is_limited_to_safe_description_formatting(self):
        d = generate(self.draft(), FakeAI())
        d['descriptions']['de']['shopify']['description'] = '<p onclick="alert(1)">Seide</p>'
        self.assertTrue(any(i['severity'] == 'error' and 'HTML' in i['message'] for i in validate_draft(d)))
        d['descriptions']['de']['shopify']['description'] = '<p>Seide <strong>53 cm</strong></p>'
        self.assertFalse(any(i['severity'] == 'error' for i in validate_draft(d)))
        d['status']='approved'
        d['descriptions']['de']['shopify']['title']='x' * 256
        with self.assertRaisesRegex(ValueError, 'validation'):
            build_export(d, self.config['export'])

    def test_export_language_separation_quotes_and_bom(self):
        d = generate(self.draft(attribute_languages=['de','pl'], description_languages=['de','pl']), FakeAI())
        value='Zażółć; "gęślą"\njaźń'
        d['descriptions']['pl']['shopify']['description']=value
        d['status']='approved'
        files=build_export(d,self.config['export'])
        self.assertEqual(len(files),6)
        content=files['Jtl_beschreibungen_import_pl.csv']
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        row=next(csv.DictReader(io.StringIO(content.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(row['shopify_description'],value)
        self.assertEqual(row['Artikelnummer'],'TEST-001')
        self.assertEqual(row['amazon_description'],'')
        attrs=list(csv.DictReader(io.StringIO(files['Jtl_attribut_import_de.csv'].decode('utf-8-sig')),delimiter=';'))
        self.assertFalse(any(row['Attributgruppe']=='JTL' for row in attrs))
        self.assertEqual(list(attrs[0]), ATTRIBUTE_COLUMNS)

    def test_column_positions_do_not_depend_on_product(self):
        headers = {}
        for category in self.config['categories']:
            for platform in ('shopify', 'amazon', 'otto'):
                d = generate(self.draft(category=category['id'], platforms=[platform]), FakeAI())
                d['status'] = 'approved'
                files = build_export(d, self.config['export'])
                for name, data in files.items():
                    header = next(csv.reader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))
                    self.assertEqual(header[:2], ['Artikelnummer', 'Artikelname'])
                    if name in headers:
                        self.assertEqual(header, headers[name], f'Header moved for {category["id"]}/{platform}/{name}')
                    headers[name] = header

    def test_configuration_additions_append_and_removals_keep_positions(self):
        old = copy.deepcopy(self.config)
        before = old['export']['master_columns'][:]
        previous_descriptions = old['export']['description_columns'][:]
        self.config['sets'][0]['rules'].append(dict(self.config['sets'][0]['rules'][0], id='JTL::New field', group='JTL', name='New field'))
        self.config['platforms'].append(dict(self.config['platforms'][0], id='newshop', name='New Shop'))
        # Even an imported reordered registry must not move existing saved positions.
        self.config['export']['master_columns'] = list(reversed(before))
        ensure_export_schema(self.config, old)
        self.assertEqual(self.config['export']['master_columns'], before + ['New field'])
        self.assertEqual(self.config['export']['description_columns'][:len(previous_descriptions)], previous_descriptions)
        updated = copy.deepcopy(self.config)
        self.config['sets'][0]['rules'] = []
        self.config['platforms'] = self.config['platforms'][1:]
        ensure_export_schema(self.config, updated)
        self.assertEqual(self.config['export']['master_columns'], updated['export']['master_columns'])
        self.assertEqual(self.config['export']['description_columns'], updated['export']['description_columns'])

    def test_no_lossy_encoding_and_no_duplicate_mapped_headers(self):
        settings=copy.deepcopy(self.config['export'])
        settings['encoding']='cp1252'
        with self.assertRaisesRegex(ValueError,'Windows-1252'):
            csv_bytes([{'x':'Łódź'}],['x'],settings)
        settings['column_mapping']={'a':'x','b':'x'}
        with self.assertRaisesRegex(ValueError,'duplicate'):
            csv_bytes([],['a','b'],settings)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=Application(Path(self.temp.name)/'data',Path(self.temp.name)/'exports',FakeAI)

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return self.app.create({'product':product()})

    def generated(self):
        d=self.create()
        result=generate(d,FakeAI())
        return self.app.store.save(result,d['revision'])

    def test_persistence_unique_sku_and_revision_conflict(self):
        d=self.create()
        with self.assertRaisesRegex(ValueError,'SKU already'):
            self.create()
        store=Store(Path(self.temp.name)/'data')
        self.assertEqual(store.get(d['id'])['input']['sku'],'TEST-001')
        body=review_payload(d)
        self.app.save_review(d['id'],body)
        with self.assertRaisesRegex(ValueError,'another window'):
            self.app.save_review(d['id'],body)

    def test_parallel_sku_reservations(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            skus=list(pool.map(lambda _: self.app.store.reserve_sku(),range(30)))
        self.assertEqual(len(set(skus)),30)

    def test_edits_invalidate_approval_and_derived_fields_follow(self):
        d=self.generated()
        approved=self.app.approve(d['id'],{'revision':d['revision'],'reviewed':True})
        payload=review_payload(approved)
        payload['attributes']['de']['Artikel::Farbe']='Rot'
        edited=self.app.save_review(d['id'],payload)
        self.assertEqual(edited['status'],'draft')
        self.assertEqual(next(r['value'] for r in edited['attributes']['de'] if r['id']=='Shopify-Attribute::Color'),'Rot')
        with self.assertRaises(ValueError):
            self.app.export(d['id'],{'revision':edited['revision']})

    def test_disk_export_and_manifest(self):
        d=self.generated()
        d=self.app.approve(d['id'],{'revision':d['revision'],'reviewed':True})
        result=self.app.export(d['id'],{'revision':d['revision']})
        directory=Path(result['directory'])
        self.assertTrue((directory/'export.zip').is_file())
        self.assertTrue((directory/'manifest.json').is_file())
        snapshot=json.loads((directory/'reviewed_product.json').read_text(encoding='utf-8'))
        self.assertNotIn('data',snapshot['input']['images'][0])
        repeated=self.app.export(d['id'],{'revision':d['revision']})
        self.assertNotEqual(result['directory'],repeated['directory'])

    def test_catalog_snapshot_is_stable(self):
        d=self.create()
        config=self.app.store.config()
        config['sets'][0]['rules'][0]['value']='Different'
        self.app.store.save_config(config)
        self.assertEqual(self.app.store.get(d['id']),d)
        with self.assertRaisesRegex(ValueError,'another window'):
            self.app.store.save_config(dict(config,version=1))

    def test_review_rejects_extra_fields_and_mutated_identity(self):
        d=self.create()
        payload=review_payload(d)
        payload['attributes']['de']['malicious']='x'
        with self.assertRaises(ValueError):
            self.app.save_review(d['id'],payload)

    def test_http_bootstrap_and_cross_origin_protection(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.app))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urllib.request.urlopen(base+'/api/bootstrap') as response:
                value=json.load(response)
            self.assertIn('config',value)
            self.assertNotIn('key',value['ai'])
            req=urllib.request.Request(base+'/api/products',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://evil.invalid','X-Local-Client':'product-studio'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code,400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__=='__main__':
    unittest.main()
