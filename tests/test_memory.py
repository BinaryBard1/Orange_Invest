"""Tests for Memory System (Stated vs Inferred, View/Correct/Forget, Persistence)."""

import os
import tempfile
import unittest
from orange_invest.agent import HomeServiceAgent
from orange_invest.memory import MemoryStore
from orange_invest.mock_api import MockProviderAPI
from orange_invest.models import FactSource, FactStatus


class TestMemorySystem(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "mem.json")
        self.mem = MemoryStore(storage_path=self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_stated_vs_inferred_provenance(self):
        uid = "mem_user_1"
        self.mem.set_stated(uid, "area", "Indiranagar")
        self.mem.set_inferred(uid, "preferred_technician", "Ramesh", confidence=0.85)

        area_fact = self.mem.get_fact(uid, "area")
        self.assertEqual(area_fact.source, FactSource.STATED)
        self.assertTrue(self.mem.is_fact_usable_automatically(uid, "area"))

        tech_fact = self.mem.get_fact(uid, "preferred_technician")
        self.assertEqual(tech_fact.source, FactSource.INFERRED)
        # Inferred facts MUST NOT be used automatically without confirmation!
        self.assertFalse(self.mem.is_fact_usable_automatically(uid, "preferred_technician"))
        self.assertTrue(self.mem.requires_confirmation(uid, "preferred_technician"))

    def test_inferred_facts_require_confirmation_in_agent(self):
        uid = "mem_user_inferred"
        self.mem.set_inferred(uid, "area", "Indiranagar")

        agent = HomeServiceAgent(memory=self.mem)
        agent.start_session(uid)

        # User starts request without specifying area
        reply = agent.handle_message("Book a plumber mornings this week under 1500")

        # Agent notices inferred preference and asks for confirmation first
        self.assertIn("noticed you previously booked in Indiranagar", reply)
        self.assertIn("Would you like to search in Indiranagar?", reply)

        # 1. If user confirms ("yes"):
        confirm_reply = agent.handle_message("yes")
        self.assertIn("Continuing with your plumbing request", confirm_reply)
        self.assertEqual(agent.state.area, "Indiranagar")
        # Inferred fact is now promoted to stated
        self.assertEqual(self.mem.get_fact(uid, "area").source, FactSource.STATED)

    def test_inferred_facts_rejection(self):
        uid = "mem_user_reject"
        self.mem.set_inferred(uid, "area", "Indiranagar")

        agent = HomeServiceAgent(memory=self.mem)
        agent.start_session(uid)

        agent.handle_message("Book a plumber mornings this week under 1500")
        # User rejects inferred area
        reject_reply = agent.handle_message("no")
        self.assertIn("won't use that preference", reject_reply)
        self.assertIsNone(agent.state.area)

    def test_view_correct_and_forget_preferences(self):
        uid = "mem_user_ops"
        self.mem.set_stated(uid, "area", "Indiranagar")
        self.mem.set_inferred(uid, "preferred_technician", "Ramesh")

        agent = HomeServiceAgent(memory=self.mem)
        agent.start_session(uid)

        # 1. View preferences
        view_reply = agent.handle_message("Show my saved preferences")
        self.assertIn("area: Indiranagar [stated by you]", view_reply)
        self.assertIn("preferred_technician: Ramesh [inferred by agent", view_reply)

        # 2. Correct preference
        self.mem.correct_fact(uid, "area", "Koramangala")
        updated_fact = self.mem.get_fact(uid, "area")
        self.assertEqual(updated_fact.value, "Koramangala")
        self.assertEqual(updated_fact.source, FactSource.STATED)

        # 3. Forget preference
        forget_reply = agent.handle_message("Forget my area preference")
        self.assertIn("forgotten your preference for 'area'", forget_reply)
        self.assertIn("never be reused", forget_reply)

        # Forgotten fact cannot be retrieved
        self.assertIsNone(self.mem.get_fact(uid, "area"))

        # Invariant: Forgotten facts are never reused or re-inferred!
        res = self.mem.set_inferred(uid, "area", "Indiranagar")
        self.assertIsNone(res, "Forgotten fact must not be re-inferred or resurrected")
        self.assertIsNone(self.mem.get_fact(uid, "area"))

    def test_cross_session_persistence(self):
        uid = "mem_user_persist"
        self.mem.set_stated(uid, "area", "Whitefield")

        # Create new MemoryStore pointing to the same storage path (simulating new session/process)
        new_store = MemoryStore(storage_path=self.db_path)
        fact = new_store.get_fact(uid, "area")
        self.assertIsNotNone(fact)
        self.assertEqual(fact.value, "Whitefield")
        self.assertEqual(fact.source, FactSource.STATED)


if __name__ == "__main__":
    unittest.main()

