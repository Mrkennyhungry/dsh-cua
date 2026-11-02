"""Input gate: cancelled actions must not send input, and a live session must pass."""

import time
import unittest
from unittest import mock

from dsh_desktop import input_gate


class LeaseTests(unittest.TestCase):
    def tearDown(self):
        input_gate.unbind()

    def test_bound_action_is_allowed_while_live(self):
        with mock.patch.object(input_gate, "session_blocker", return_value=None):
            token = input_gate.begin_action(5)
            input_gate.bind(token)
            self.assertIsNone(input_gate.guard())
            input_gate.end_action(token)

    def test_cancelled_action_is_refused_even_after_end(self):
        with mock.patch.object(input_gate, "session_blocker", return_value=None):
            token = input_gate.begin_action(5)
            input_gate.bind(token)
            input_gate.cancel_action(token)
            blocked = input_gate.guard()
            self.assertIsNotNone(blocked)
            self.assertIn("INPUT_CANCELLED", blocked["error"])
            # end_action runs while the worker thread may still be alive.
            input_gate.end_action(token)
            self.assertIn("INPUT_CANCELLED", input_gate.guard()["error"])

    def test_cancellation_is_per_action(self):
        with mock.patch.object(input_gate, "session_blocker", return_value=None):
            doomed = input_gate.begin_action(5)
            fresh = input_gate.begin_action(5)
            input_gate.cancel_action(doomed)
            input_gate.bind(fresh)
            self.assertIsNone(input_gate.guard())
            input_gate.bind(doomed)
            self.assertIn("INPUT_CANCELLED", input_gate.guard()["error"])
            input_gate.end_action(doomed)
            input_gate.end_action(fresh)

    def test_expired_deadline_is_refused(self):
        with mock.patch.object(input_gate, "session_blocker", return_value=None):
            token = input_gate.begin_action(0)
            input_gate.bind(token)
            time.sleep(0.01)
            with mock.patch.object(input_gate, "_DEADLINE_GRACE_S", -1.0):
                input_gate.cancel_action(token)
            blocked = input_gate.guard()
            self.assertIsNotNone(blocked)
            input_gate.end_action(token)

    def test_session_blocker_wins_when_action_is_fine(self):
        refusal = {"success": False, "error": "SESSION_SECURE_DESKTOP: locked", "input_refused": True}
        with mock.patch.object(input_gate, "session_blocker", return_value=refusal):
            token = input_gate.begin_action(5)
            input_gate.bind(token)
            self.assertEqual(input_gate.guard()["error"], refusal["error"])
            input_gate.end_action(token)

    def test_run_bound_binds_and_clears_the_token(self):
        seen = {}

        def probe():
            seen["token"] = input_gate.current_token()
            return "result"

        token = input_gate.begin_action(5)
        self.assertEqual(input_gate.run_bound(token, probe), "result")
        self.assertEqual(seen["token"], token)
        self.assertIsNone(input_gate.current_token())
        input_gate.end_action(token)


class SessionTests(unittest.TestCase):
    def test_an_unlocked_desktop_passes(self):
        # On CI (or a locked machine) this may legitimately refuse; only assert
        # the shape, not the environment.
        result = input_gate.session_blocker()
        if result is not None:
            self.assertIn("input_refused", result)
            self.assertTrue(result["error"].split(":")[0].startswith("SESSION_"))


if __name__ == "__main__":
    unittest.main()
