import unittest
from dsh_desktop.tool_decorator import tool


class ToolLogRedactionTests(unittest.TestCase):
    def test_text_and_nested_step_values_are_not_written_to_logs(self):
        @tool('test')
        def action(window_id: str, text: str, steps: list):
            return {'success': True}
        with self.assertLogs('dsh_desktop.tool_decorator', level='INFO') as captured:
            result = action('win-test', 'private-message-123', [
                {'action': 'type', 'text': 'nested-private-456'},
                {'action': 'click', 'name': 'private-person-789'},
            ])
        joined = '\n'.join(captured.output)
        self.assertTrue(result['success'])
        self.assertIn('win-test', joined)
        self.assertIn('<redacted>', joined)
        for sensitive in ('private-message-123', 'nested-private-456', 'private-person-789'):
            self.assertNotIn(sensitive, joined)


if __name__ == '__main__':
    unittest.main()
