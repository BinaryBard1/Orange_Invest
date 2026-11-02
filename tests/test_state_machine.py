"""Tests for Explicit Workflow State and invariant enforcement."""

import unittest
from orange_invest.agent import HomeServiceAgent
from orange_invest.mock_api import MockProviderAPI
from orange_invest.models import WorkflowStatus


class TestWorkflowState(unittest.TestCase):

    def setUp(self):
        self.api = MockProviderAPI()
        self.agent = HomeServiceAgent(api=self.api)
        self.agent.start_session("test_user_state")

    def test_asks_only_for_missing_fields_step_by_step(self):
        # Turn 1: User provides only service ("plumber")
        reply1 = self.agent.handle_message("Find a plumber for my kitchen tap")
        self.assertEqual(self.agent.state.service, "plumbing")
        self.assertIsNone(self.agent.state.area)
        self.assertIsNone(self.agent.state.time_window)
        # Agent should ask for area (the first missing field)
        self.assertIn("area", reply1.lower())

        # Turn 2: User provides area ("Indiranagar")
        reply2 = self.agent.handle_message("In Indiranagar")
        # Service MUST NOT be re-asked!
        self.assertEqual(self.agent.state.service, "plumbing")
        self.assertEqual(self.agent.state.area, "Indiranagar")
        # Agent should now ask for time window
        self.assertIn("schedule", reply2.lower())

        # Turn 3: User provides time window ("mornings this week under 1500")
        reply3 = self.agent.handle_message("mornings this week under 1500")
        self.assertIsNotNone(self.agent.state.time_window)
        self.assertEqual(self.agent.state.budget, 1500.0)
        # Now all facts are known, agent should have searched and proposed an option
        self.assertEqual(self.agent.state.status, WorkflowStatus.AWAITING_APPROVAL)
        self.assertIn("Ramesh Plumbing Works", reply3)

    def test_correction_to_one_detail_preserves_everything_else(self):
        # Provide full booking request
        self.agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        self.assertEqual(self.agent.state.service, "plumbing")
        self.assertEqual(self.agent.state.area, "Indiranagar")
        self.assertEqual(self.agent.state.budget, 1500.0)

        # User corrects only budget
        reply = self.agent.handle_message("Actually, change my budget to ₹2000")
        self.assertEqual(self.agent.state.budget, 2000.0)
        # Known facts preserved:
        self.assertEqual(self.agent.state.service, "plumbing")
        self.assertEqual(self.agent.state.area, "Indiranagar")
        self.assertIsNotNone(self.agent.state.time_window)

    def test_never_re_asks_known_facts(self):
        # User provides service and area in one sentence
        reply = self.agent.handle_message("I need an electrician in Koramangala")
        self.assertEqual(self.agent.state.service, "electrician")
        self.assertEqual(self.agent.state.area, "Koramangala")
        # Should ask for schedule/time window, NOT service or area
        self.assertNotIn("what service", reply.lower())
        self.assertNotIn("which neighborhood", reply.lower())
        self.assertIn("when", reply.lower())


if __name__ == "__main__":
    unittest.main()

