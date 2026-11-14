import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class StreamlitDemoTests(unittest.TestCase):
    def test_demo_login_and_buyer_workspace_render(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        app = AppTest.from_file(str(app_path), default_timeout=90).run(timeout=90)
        self.assertFalse(app.exception)
        self.assertTrue(any("Find the right" in block.value for block in app.markdown))
        app.button(key="public-login").click().run(timeout=90)
        username = next(widget for widget in app.text_input if widget.label == "Username or email")
        username.set_value("ui.test.buyer")
        next(button for button in app.button if button.label == "Continue to demo").click().run(timeout=90)
        self.assertFalse(app.exception)
        self.assertTrue(any("Search Property" in title.value for title in app.title))


if __name__ == "__main__":
    unittest.main()
