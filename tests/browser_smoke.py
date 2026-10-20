"""Optional end-to-end test using Chromium and intercepted local fixture responses."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

import pinterest_scraper as scraper


class BrowserSmokeTest(unittest.TestCase):
    def test_browser_collection_download_and_exports(self):
        output = io.BytesIO()
        Image.new("RGB", (24, 32), "gold").save(output, format="PNG")
        image = output.getvalue()
        html = '''<html><body>
          <div data-test-id="pin-visual-wrapper"><a href="/pin/123/">
            <img src="https://i.pinimg.com/236x/123.jpg" alt="gold necklace"></a></div>
          <a href="/pin/456/"><div data-test-id="pin-visual-wrapper">
            <img src="https://i.pinimg.com/236x/456.jpg" alt="silver necklace"></div></a>
          <a href="/pin/123/"><img src="https://i.pinimg.com/236x/123.jpg" alt="duplicate"></a>
        </body></html>'''
        real_goto = scraper.goto_with_retries
        def fixture_goto(page, url):
            def respond(route):
                if route.request.resource_type == "image":
                    route.fulfill(status=200, content_type="image/png", body=image)
                else:
                    route.fulfill(status=200, content_type="text/html", body=html)
            page.route("**/*", respond)
            real_goto(page, url)
            page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
        client_type = httpx.Client
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(scraper, "goto_with_retries", side_effect=fixture_goto), \
                 patch.object(scraper.time, "sleep"), \
                 patch.object(scraper.httpx, "Client", side_effect=lambda **kwargs: client_type(
                     transport=httpx.MockTransport(lambda request: httpx.Response(
                         200, content=image, headers={"content-type": "image/png"})), **kwargs)):
                result = scraper.main(["--query", "gold & silver", "--limit", "2", "--download",
                                       "--headless", "--out", directory])
            self.assertEqual(result, 0)
            folder, = Path(directory).iterdir()
            rows = json.loads((folder / "pins.json").read_text())
            self.assertEqual({row["pin_id"] for row in rows}, {"123", "456"})
            self.assertTrue(all(row["download_status"] == "downloaded" for row in rows))
            self.assertEqual(len(list((folder / "images").glob("*.png"))), 2)
            run = json.loads((folder / "run.json").read_text())
            self.assertEqual(run["downloaded"], 2)
            self.assertEqual(run["failed"], 0)
            self.assertEqual(run["options"]["aspect"], [0.0, None])
            self.assertEqual(len((folder / "pins.csv").read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
