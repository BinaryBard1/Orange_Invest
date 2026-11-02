"""Tests for Bounded Execution (Hard caps on turns, tools, time)."""

import time
import unittest
from orange_invest.agent import AgentExecutionLimits, HomeServiceAgent
from orange_invest.mock_api import MockProviderAPI
from orange_invest.models import WorkflowStatus


class TestBoundedExecution(unittest.TestCase):

    def test_max_model_turns_limit_exhaustion(self):
        # Configure tight turn limit of 2 turns
        limits = AgentExecutionLimits(max_model_turns=2)
        agent = HomeServiceAgent(limits=limits)
        agent.start_session("bound_user_turns")

        reply1 = agent.handle_message("Hello")
        reply2 = agent.handle_message("Need a plumber")

        # Turn 3 exceeds max_model_turns
        reply3 = agent.handle_message("In Indiranagar")
        self.assertIn("Maximum interaction turns (2) exceeded", reply3)
        self.assertIn("No unauthorized bookings were made", reply3)
        self.assertEqual(agent.state.status, WorkflowStatus.FAILED)

    def test_max_tool_calls_limit_exhaustion(self):
        # Configure tight tool calls limit of 1
        limits = AgentExecutionLimits(max_tool_calls=1)
        agent = HomeServiceAgent(limits=limits)
        agent.start_session("bound_user_tools")

        # Search flow normally calls search_providers + get_provider + hold_slot (3 calls)
        reply = agent.handle_message("Find a plumber in Indiranagar mornings this week under 1500")
        self.assertIn("Maximum tool calls limit (1) exceeded", reply)
        self.assertIn("No unauthorized bookings were made", reply)
        self.assertEqual(agent.state.status, WorkflowStatus.FAILED)

    def test_wall_clock_timeout_exhaustion(self):
        # Configure very short wall clock limit (0.01s)
        limits = AgentExecutionLimits(max_wall_clock_seconds=0.01)
        agent = HomeServiceAgent(limits=limits)
        agent.start_session("bound_user_time")

        # Sleep slightly to exceed timeout
        time.sleep(0.02)

        reply = agent.handle_message("Find a plumber in Indiranagar")
        self.assertIn("Wall-clock time limit", reply)
        self.assertIn("exceeded", reply)
        self.assertEqual(agent.state.status, WorkflowStatus.FAILED)

    def test_exhausted_retries_on_service_unavailable(self):
        api = MockProviderAPI()
        # Make cancel fail 503 repeatedly
        def always_503(b_id, idem_key):
            from orange_invest.mock_api import ServiceUnavailable503Error
            raise ServiceUnavailable503Error("Continuous 503")

        api.cancel = always_503
        agent = HomeServiceAgent(api=api)
        agent.start_session("user_123")

        agent.handle_message("Cancel my AC servicing")
        reply = agent.handle_message("yes")
        self.assertIn("Unable to cancel booking due to provider upstream downtime", reply)
        self.assertEqual(agent.state.status, WorkflowStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
