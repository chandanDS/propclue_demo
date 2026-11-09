import unittest
from datetime import datetime, timedelta

from database import Event, LOCALITIES, score_from_events


class DemoLogicTests(unittest.TestCase):
    def test_intent_score_explains_recent_engagement(self):
        now = datetime.utcnow()
        events = [
            Event(event_type="Enquiry submitted", event_at=now),
            Event(event_type="Project shortlisted", event_at=now),
            Event(event_type="Property compared", event_at=now),
        ]
        score, category, reasons = score_from_events(events)
        self.assertGreaterEqual(score, 45)
        self.assertEqual(category, "Medium")
        self.assertTrue(any("Enquiry submitted" in reason for reason in reasons))

    def test_old_activity_decays(self):
        now = datetime.utcnow()
        recent = [Event(event_type="Enquiry submitted", event_at=now)]
        old = [Event(event_type="Enquiry submitted", event_at=now - timedelta(days=180))]
        self.assertGreater(score_from_events(recent)[0], score_from_events(old)[0])

    def test_required_demo_geographies_are_present_and_ncr_is_separate(self):
        names = {(city, scope, name) for city, scope, name, *_ in LOCALITIES}
        self.assertIn(("Bengaluru", "City", "Whitefield"), names)
        self.assertIn(("Delhi", "City", "Dwarka"), names)
        self.assertIn(("Delhi", "NCR", "Gurugram"), names)


if __name__ == "__main__":
    unittest.main()
