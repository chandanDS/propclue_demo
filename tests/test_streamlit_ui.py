import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class StreamlitDemoTests(unittest.TestCase):
    def test_demo_login_and_buyer_workspace_render(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        app = AppTest.from_file(str(app_path), default_timeout=90).run(timeout=90)
        self.assertFalse(app.exception)
        self.assertTrue(any("Understand the property" in title.value for title in app.title))
        app.button(key="demo-role-buyer").click().run(timeout=90)
        self.assertFalse(app.exception)
        self.assertTrue(any("Market pulse" in title.value for title in app.title))


if __name__ == "__main__":
    unittest.main()
