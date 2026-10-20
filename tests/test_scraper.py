"""Offline regression tests; no Pinterest requests or browser binaries needed."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

import pinterest_scraper as scraper


def image_bytes(fmt="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (24, 32), "gold").save(output, format=fmt)
    return output.getvalue()


def pin(pin_id="123", width=236, height=354, title="gold necklace"):
    return dict(href=f"/pin/{pin_id}/", src=f"https://i.pinimg.com/236x/{pin_id}.jpg",
                title=title, alt=title, w=width, h=height)


class FakePage:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.mouse = self

    def evaluate(self, script):
        return next(self.batches, [])

    def wheel(self, *args):
        pass


class CollectionTests(unittest.TestCase):
    def collect(self, batches, limit=1, **kwargs):
        with patch.object(scraper.time, "sleep"):
            return scraper.scroll_and_collect(FakePage(batches), limit, **kwargs)

    def test_lazy_image_is_reconsidered(self):
        rows = self.collect([[pin(width=0, height=0)], [pin()]], min_area=100)
        self.assertEqual([row["pin_id"] for row in rows], ["123"])

    def test_unknown_dimensions_do_not_bypass_aspect_filter(self):
        self.assertEqual(self.collect([[pin(width=0, height=0)]], aspect_min=1.5), [])

    def test_deduplicates_pins_and_honors_limit(self):
        rows = self.collect([[pin(), pin(), pin("456"), pin("789")]], limit=2)
        self.assertEqual({row["pin_id"] for row in rows}, {"123", "456"})

    def test_filters_are_explicit_case_insensitive_substrings(self):
        self.assertEqual(len(self.collect([[pin(title="Tips for GOLD necklaces")]])), 1)
        self.assertEqual(len(self.collect([[pin()]], require_keywords=("GOLD", "silver"))), 1)
        self.assertEqual(self.collect([[pin()]], avoid_words=("GOLD",)), [])

    def test_rejects_foreign_hosts_and_malformed_pin_paths(self):
        invalid_image = pin()
        invalid_image["src"] = "https://i.pinimg.com.example.org/a.jpg"
        invalid_pin = pin()
        invalid_pin["href"] = "https://example.org/pin/123/"
        invalid_path = pin()
        invalid_path["href"] = "/pin/not-a-number/"
        self.assertEqual(self.collect([[invalid_image, invalid_pin, invalid_path]]), [])

    def test_stops_after_ten_idle_passes(self):
        page = FakePage([])
        with patch.object(scraper.time, "sleep"), patch.object(page, "evaluate", return_value=[]) as evaluate:
            self.assertEqual(scraper.scroll_and_collect(page, 2), [])
            self.assertEqual(evaluate.call_count, 10)


class ArgumentTests(unittest.TestCase):
    def test_query_roundtrips_special_characters(self):
        query = "gold & silver #rings + café"
        parsed = urlsplit(scraper.build_search_url(query))
        self.assertEqual(parse_qs(parsed.query), {"q": [query]})
        self.assertEqual(parsed.fragment, "")

    def test_invalid_arguments_exit_before_browser_launch(self):
        cases = [[], ["--download"], ["--query", " "],
                 ["--query", "gold", "--url", "https://pinterest.com"],
                 ["--query", "gold", "--limit", "0"],
                 ["--query", "gold", "--minsize", "-1"],
                 ["--query", "gold", "--aspect", "2,1"],
                 ["--query", "gold", "--aspect", "nan,2"],
                 ["--query", "gold", "--aspect", "1"],
                 ["--url", "https://pinterest.com.evil.test/search"],
                 ["--url", "http://pinterest.com/search"]]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    scraper.parse_args(args)
                self.assertEqual(caught.exception.code, 2)

    def test_no_implicit_downloads_or_filters(self):
        args = scraper.parse_args(["--query", "gold"])
        self.assertFalse(args.download)
        self.assertEqual(args.avoid, "")
        self.assertEqual(args.limit, 80)

    def test_variant_order_and_duplicate_removal(self):
        self.assertEqual(scraper.upgraded_url_candidates("https://i.pinimg.com/236x/a.jpg"), [
            "https://i.pinimg.com/originals/a.jpg", "https://i.pinimg.com/736x/a.jpg",
            "https://i.pinimg.com/236x/a.jpg"])
        self.assertEqual(len(scraper.upgraded_url_candidates("https://i.pinimg.com/originals/a.jpg")), 1)


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("connection lost")


class DownloadTests(unittest.TestCase):
    def run_download(self, handler, max_bytes=25_000_000):
        client_type = httpx.Client
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "images"
            rows = [{"pin_id": "123", "image_url": "https://i.pinimg.com/236x/a.jpg"}]
            with patch.object(scraper.httpx, "Client", side_effect=lambda **kwargs: client_type(
                    transport=httpx.MockTransport(handler), **kwargs)), patch.object(scraper.time, "sleep"):
                summary = scraper.download_images(rows, folder, max_bytes=max_bytes)
            return rows[0], summary, {path.name: path.read_bytes() for path in folder.iterdir()}

    def test_verified_image_records_actual_dimensions_and_format(self):
        data = image_bytes("PNG")
        row, summary, files = self.run_download(lambda request: httpx.Response(
            200, content=data, headers={"content-type": "image/png"}))
        self.assertEqual(summary, {"downloaded": 1, "failed": 0})
        self.assertEqual(files, {"123.png": data})
        self.assertEqual((row["download_w"], row["download_h"]), (24, 32))
        self.assertEqual(row["local_path"], "images/123.png")
        self.assertEqual(row["download_bytes"], len(data))

    def test_broken_stream_retries_without_leaving_partial_files(self):
        requests = []
        data = image_bytes()
        def handler(request):
            requests.append(str(request.url))
            if "/originals/" in str(request.url):
                return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "image/jpeg"})
            return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})
        row, summary, files = self.run_download(handler)
        self.assertEqual(len(requests), 2)
        self.assertEqual(files, {"123.jpg": data})
        self.assertEqual(row["download_status"], "downloaded")
        self.assertEqual(row["download_error"], "")

    def test_rejects_nonimages_and_corrupt_images(self):
        for content_type, data in [("text/html", b"<html>login</html>"),
                                   ("image/jpeg", b"not an image"),
                                   ("image/jpeg", image_bytes()[:-20])]:
            with self.subTest(content_type=content_type, size=len(data)):
                row, summary, files = self.run_download(lambda request: httpx.Response(
                    200, content=data, headers={"content-type": content_type}))
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(files, {})
                self.assertTrue(row["download_error"])

    def test_size_limit_rejects_declared_and_streamed_oversize(self):
        data = image_bytes()
        for headers in ({"content-type": "image/jpeg", "content-length": str(len(data))},
                        {"content-type": "image/jpeg"}):
            with self.subTest(headers=headers):
                row, summary, files = self.run_download(lambda request: httpx.Response(
                    200, stream=httpx.ByteStream(data), headers=headers), max_bytes=10)
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(files, {})

    def test_404_falls_back_and_failure_is_reported(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(404)
        row, summary, files = self.run_download(handler)
        self.assertEqual(len(requests), 3)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(files, {})

    def test_content_length_mismatch_is_rejected(self):
        row, summary, files = self.run_download(lambda request: httpx.Response(
            200, stream=httpx.ByteStream(image_bytes()),
            headers={"content-type": "image/jpeg", "content-length": "10000"}))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(files, {})

    def test_redirects_are_not_followed_to_untrusted_hosts(self):
        requests = []
        def handler(request):
            requests.append(request.url.host)
            return httpx.Response(302, headers={"location": "https://example.com/a.jpg"})
        row, summary, files = self.run_download(handler)
        self.assertEqual(set(requests), {"i.pinimg.com"})
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(files, {})

    def test_keyboard_interrupt_removes_temporary_file(self):
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                raise KeyboardInterrupt
                yield b""
        client_type = httpx.Client
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(scraper.httpx, "Client", side_effect=lambda **kwargs: client_type(
                    transport=httpx.MockTransport(lambda request: httpx.Response(
                        200, stream=Interrupted(), headers={"content-type": "image/jpeg"})), **kwargs)):
                with self.assertRaises(KeyboardInterrupt):
                    scraper.download_images([{"pin_id": "123", "image_url": "https://i.pinimg.com/236x/a.jpg"}], folder)
            self.assertEqual(list(folder.iterdir()), [])


class OutputTests(unittest.TestCase):
    def test_run_directories_are_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            first = scraper.smart_out_dir(directory, "gold / necklaces")
            marker = first / "keep.txt"
            marker.write_text("original")
            second = scraper.smart_out_dir(directory, "gold / necklaces")
            self.assertNotEqual(first, second)
            self.assertEqual(marker.read_text(), "original")
            self.assertEqual(second.parent, Path(directory))

    def test_empty_exports_have_stable_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            scraper.save_metadata([], folder)
            self.assertEqual(json.loads((folder / "pins.json").read_text()), [])
            with (folder / "pins.csv").open() as source:
                reader = csv.DictReader(source)
                self.assertEqual(reader.fieldnames, list(scraper.FIELDS))
                self.assertEqual(list(reader), [])

    def test_pin_ids_remain_strings_in_json(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            scraper.save_metadata([{"pin_id": "987654321012345678"}], folder)
            self.assertEqual(json.loads((folder / "pins.json").read_text())[0]["pin_id"], "987654321012345678")


if __name__ == "__main__":
    unittest.main()
