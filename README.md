# Pinterest Scraper

A Python command-line tool that collects image references from public Pinterest search pages and optionally downloads available image variants. Useful for personal visual research and reference gathering.

It uses Playwright to read rendered pages, HTTPX to download images, and Pillow to validate downloaded files. No API credentials are required.

## Requirements

- Python 3.11 or newer
- Playwright's Chromium browser
- Network access to Pinterest and its image CDN

## Installation

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

On Windows, activate with `.venv\Scripts\activate` instead. On Linux, you may need `python -m playwright install --with-deps chromium` to install browser system dependencies.

## Usage

Collect up to 80 image references and export metadata:

```bash
python pinterest_scraper.py --query "winter landscape" --headless
```

Also download images, trying original-resolution variants first:

```bash
python pinterest_scraper.py --query "gold jewelry" --limit 100 --download --prefer-originals --headless
```

Filter using page text and observed thumbnail dimensions:

```bash
python pinterest_scraper.py --query "winter landscape" \
  --filter "snow,mountains" --avoid "quote,template" \
  --aspect "0.5,1.5" --minsize 50000 --download --headless
```

Read a public Pinterest page directly:

```bash
python pinterest_scraper.py --url "https://www.pinterest.com/search/pins/?q=forest" --headless
```

Run without `--headless` to see the browser. The tool does not sign in, dismiss consent choices, remove login prompts, or bypass access restrictions. A login prompt or unavailable public feed may prevent collection.

### Options

Exactly one of `--query` or `--url` is required. Running with no arguments shows a usage error and does not start a scrape.

| Option | Behavior |
| --- | --- |
| `--query TEXT` | Search text; special characters are URL-encoded |
| `--url URL` | Public HTTPS page on `pinterest.com` or its subdomains |
| `--limit N` | Maximum unique pins; positive integer, default 80 |
| `--out PATH` | Base output directory, default `out` |
| `--download` | Download images; otherwise export metadata only |
| `--headless` | Hide the browser window |
| `--filter WORDS` | Comma-separated substrings; accept **any** match in title/alt |
| `--avoid WORDS` | Comma-separated substrings; reject any match; empty by default |
| `--aspect MIN,MAX` | Inclusive observed width/height range; no restriction by default |
| `--minsize N` | Minimum observed width × height in pixels; default 0 |
| `--prefer-originals` | Try `/originals/`, then `/736x/`, then the observed URL |
| `--verbose` | Include diagnostic logging for failed candidates |

Text matching is case insensitive. There are no hidden word exclusions. Without `--prefer-originals`, downloads try the 736px variant before the observed URL. These are requests for existing CDN variants, not image upscaling.

### Output

Each invocation that completes collection creates a unique directory:

```text
out/
  winter_landscape_2026-10-20_143000_<unique-suffix>/
    pins.csv
    pins.json
    run.json
    images/          # when downloads are requested
      <pin-id>.jpg
```

Previous run directories are preserved. Image extensions are determined from the decoded image format, not the URL. JPEG, PNG, WebP, and GIF are supported. Downloads are sequential, with short pauses between pins and scrolls.

Each pin record contains:

- `pin_id`, `pin_url`, `image_url`: the pin and observed image reference.
- `title`, `alt`: extracted image alternative text; `title` is currently an alias for `alt`, not an independently extracted pin title.
- `img_w`, `img_h`: dimensions observed in the page. Filters and sorting use these thumbnail dimensions.
- `download_status`: `not_requested`, `pending`, `downloaded`, or `failed`.
- `download_url`, `local_path`, `download_w`, `download_h`, `download_bytes`, `download_error`: download results, populated when applicable. Paths are relative to the run directory.

Downloaded dimensions may be much larger than `img_w` and `img_h`. JSON preserves pin IDs as strings; when importing CSV into a spreadsheet, import the ID column as text to avoid rounding long IDs.

`run.json` records the target URL, options, UTC completion timestamp, record count, and download success/failure counts. An unrestricted maximum aspect ratio is represented as `null`. CSV and JSON are saved before downloading, then updated with download outcomes. On an interrupted download, metadata is saved and temporary files are removed; a completed run summary may not be present.

Exit codes:

- `0`: at least one pin collected and all requested downloads succeeded.
- `1`: no pins collected, a run error, or one or more failed downloads.
- `2`: invalid command-line arguments.
- `130`: interrupted by the user.

A run below the requested limit warns but can still exit successfully. The limit is a maximum, not a guaranteed result count.

## Limitations

- Pinterest's page structure, public availability, and CDN behavior can change. Selectors may need maintenance.
- Collection stops after ten consecutive passes without accepted additions; selective filters can yield fewer results.
- Deduplication is by pin ID within a run, not by image content or across runs.
- It collects images and video thumbnails, not video files, creators, boards, engagement metrics, or original source attribution.
- Browser sessions are temporary. There is no persistent authentication, scheduling, concurrent downloading, or resume command.
- HTTP redirects are rejected for downloads. Failed candidates fall back to the next known CDN variant; failures are reported in metadata and logs.
- Images must fit the 25 MB download limit and pass Pillow verification and decoding before being atomically moved into place. Invalid or incomplete files are discarded.

## Development

The regression suite uses local fixtures and mocked HTTP responses. It does not contact Pinterest or require a browser download:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs it on Python 3.11 and 3.14. Direct dependency versions are pinned in `requirements.txt`; transitive dependencies are resolved by pip.

An optional end-to-end smoke test launches Chromium and intercepts all page and image responses with local fixtures. It requires the browser installation from the setup instructions, but does not contact Pinterest:

```bash
python -m unittest discover -s tests -p browser_smoke.py -v
```

## Publishing and image rights

The default output folder, virtual environments, caches, and common credential files are excluded by `.gitignore`. If you choose another output directory inside the repository, exclude it before committing. Never commit downloaded image libraries, browser sessions, or credentials.

Use only where you have permission, check applicable site rules, and keep request volumes modest. Downloading does not grant rights to reuse an image. The code license does not cover images obtained from Pinterest.

## License

[MIT](LICENSE).
