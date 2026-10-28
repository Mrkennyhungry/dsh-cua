import unittest
from pathlib import Path
from dsh_desktop import web_components


class ComponentCatalogTests(unittest.TestCase):
    def test_missing_catalog_has_no_product_specific_signatures(self):
        self.assertEqual(web_components._load_component_catalog(Path('does-not-exist.json')), ((), ()))

    def test_private_catalog_loads_component_families_and_content_rules(self):
        path = Path(__file__).resolve().parent.parent / 'private-knowledge-source' / 'console-components.json'
        prefixes, content = web_components._load_component_catalog(path)
        self.assertIn('app-ioa-table', prefixes)
        self.assertIn('stat-metric', content)


if __name__ == '__main__':
    unittest.main()
