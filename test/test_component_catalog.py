import json
import tempfile
import unittest
from pathlib import Path
from dsh_desktop import web_components


class ComponentCatalogTests(unittest.TestCase):
    def test_missing_catalog_has_no_product_specific_signatures(self):
        self.assertEqual(web_components._load_component_catalog(Path('does-not-exist.json')), ((), ()))

    def test_optional_catalog_loads_component_families_and_content_rules(self):
        catalog = {
            'entries': [{'family': 'demo-table'}, {'family': 'demo'}, {'family': ''}],
            'content_prefixes': ['stat-metric'],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'console-components.json'
            path.write_text(json.dumps(catalog), encoding='utf-8')
            prefixes, content = web_components._load_component_catalog(path)
        self.assertEqual(prefixes, ('demo-table', 'demo'))
        self.assertEqual(content, ('stat-metric',))


if __name__ == '__main__':
    unittest.main()
