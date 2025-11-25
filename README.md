# Pinterest Scraper

A simple Pinterest image scraper that uses Playwright and Python to download images from Pinterest search results.

## Installation

### 1. Clone the repository and set up the environment
```bash
git clone <your-repo-url>
cd pinterest-scraper

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
python3 -m playwright install chromium

# Run the scraper
python3 pinterest-scraper.py
