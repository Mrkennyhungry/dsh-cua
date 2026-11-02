"""Telemetry must stay off unless someone turns it on explicitly."""

import unittest
from unittest import mock

from dsh_desktop import telemetry


class TelemetryDefaultTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(telemetry.enabled)

    def test_nothing_is_sent_while_disabled(self):
        with mock.patch.object(telemetry.urllib_request, "urlopen") as urlopen:
            telemetry.send_event("app_start", version="test")
            urlopen.assert_not_called()
            telemetry.track_tool_error("test", "desktop_click", "tool_error", error_detail="x")
            urlopen.assert_not_called()

    def test_enabling_sends_one_request(self):
        thread = mock.MagicMock()
        with mock.patch.object(telemetry, "enabled", True), \
                mock.patch.object(telemetry.urllib_request, "urlopen") as urlopen, \
                mock.patch.object(telemetry.threading, "Thread", return_value=thread) as thread_cls:
            telemetry.send_event("app_start", version="test")
            thread_cls.assert_called_once()
            thread.start.assert_called_once()
            run = thread_cls.call_args.kwargs["target"]
            run()
            urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
