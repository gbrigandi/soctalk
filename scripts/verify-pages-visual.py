#!/usr/bin/env python3
"""
Visual Page Verification Script using Anthropic Claude Vision

This script captures screenshots of all SocTalk frontend pages and uses
Claude's vision capabilities to verify they render correctly.

Requirements:
- anthropic Python package
- playwright Python package
- ANTHROPIC_API_KEY environment variable

Usage:
    python scripts/verify-pages-visual.py
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

# Check for required packages
try:
    import anthropic
    from playwright.async_api import async_playwright
except ImportError as e:
    print(f"Missing required package: {e}")
    print("Install with: pip install anthropic playwright")
    sys.exit(1)


FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
API_URL = os.getenv("API_URL", "http://localhost:8000")

# Pages to verify
PAGES = [
    {
        "name": "Dashboard",
        "path": "/",
        "expected_elements": [
            "Navigation sidebar with links",
            "Dashboard title or header",
            "KPI cards or metrics display",
            "Charts or data visualizations",
            "No error messages or 503/500 errors",
        ],
    },
    {
        "name": "Investigations",
        "path": "/investigations",
        "expected_elements": [
            "Navigation sidebar",
            "Investigations title/header",
            "Table or list of investigations (may be empty)",
            "Search or filter controls",
            "No error messages or 503/500 errors",
        ],
    },
    {
        "name": "Analytics",
        "path": "/analytics",
        "expected_elements": [
            "Navigation sidebar",
            "Analytics title/header",
            "Charts or graphs",
            "Statistics or metrics",
            "No error messages or 503/500 errors",
        ],
    },
    {
        "name": "Audit",
        "path": "/audit",
        "expected_elements": [
            "Navigation sidebar",
            "Audit title/header",
            "Audit log table or list",
            "Filter or search options",
            "No error messages or 503/500 errors",
        ],
    },
    {
        "name": "Review",
        "path": "/review",
        "expected_elements": [
            "Navigation sidebar",
            "Review title/header",
            "Pending reviews list or empty state",
            "Action buttons if items present",
            "No error messages or 503/500 errors",
        ],
    },
]


async def capture_screenshot(page, url: str, name: str) -> bytes:
    """Capture a screenshot of the given URL."""
    print(f"  Navigating to {url}...")
    await page.goto(url)

    # Wait for page to load
    await page.wait_for_load_state("domcontentloaded")

    # Wait for any loading spinners to disappear
    try:
        await page.wait_for_selector(".animate-spin", state="hidden", timeout=10000)
    except Exception:
        pass  # No spinner or already hidden

    # Additional wait for dynamic content
    await asyncio.sleep(1)

    print(f"  Capturing screenshot...")
    screenshot = await page.screenshot(full_page=True)
    return screenshot


def analyze_screenshot_with_claude(
    client: anthropic.Anthropic,
    screenshot_base64: str,
    page_name: str,
    expected_elements: list[str],
) -> dict:
    """Use Claude Vision to analyze the screenshot."""

    prompt = f"""Analyze this screenshot of the "{page_name}" page from a SOC (Security Operations Center) web application called SocTalk.

Expected elements for this page:
{chr(10).join(f"- {elem}" for elem in expected_elements)}

Please analyze the screenshot and provide:

1. **Page Renders Correctly**: Yes/No - Does the page appear to render properly without errors?

2. **Elements Present**: List which expected elements are visible in the screenshot.

3. **Missing Elements**: List any expected elements that are NOT visible.

4. **Error Detection**: Are there any visible error messages, HTTP error codes (like 503, 500, 404), or broken UI elements?

5. **Overall Assessment**:
   - PASS: Page renders correctly with no errors
   - WARN: Page renders but has minor issues
   - FAIL: Page has errors or fails to render properly

6. **Details**: Any additional observations about the page rendering.

Be concise but thorough. Focus on whether the page is functional and displays correctly."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    )

    return {
        "page": page_name,
        "analysis": response.content[0].text,
    }


async def verify_api_health() -> bool:
    """Check if the API is healthy."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_URL}/health", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status") == "healthy"
    except Exception as e:
        print(f"API health check failed: {e}")
    return False


async def main():
    """Main function to verify all pages."""
    print("=" * 60)
    print("SocTalk Visual Page Verification")
    print("=" * 60)
    print()

    # Check for API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    # Initialize Anthropic client
    client = anthropic.Anthropic(api_key=api_key)

    # Check API health
    print("Checking API health...")
    try:
        import aiohttp
    except ImportError:
        print("Installing aiohttp for API health check...")
        os.system("pip install aiohttp")
        import aiohttp

    if not await verify_api_health():
        print(f"WARNING: API at {API_URL} is not healthy or not running")
        print("Pages may show errors. Continue anyway? (y/n)")
        if input().lower() != "y":
            sys.exit(1)
    else:
        print(f"API at {API_URL} is healthy")

    print()
    print(f"Frontend URL: {FRONTEND_URL}")
    print(f"API URL: {API_URL}")
    print()

    # Create screenshots directory
    screenshots_dir = Path("screenshots")
    screenshots_dir.mkdir(exist_ok=True)

    results = []

    async with async_playwright() as p:
        print("Launching browser...")
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})

        for i, page_config in enumerate(PAGES, 1):
            print()
            print(f"[{i}/{len(PAGES)}] Verifying: {page_config['name']}")
            print("-" * 40)

            url = f"{FRONTEND_URL}{page_config['path']}"

            try:
                # Capture screenshot
                screenshot_bytes = await capture_screenshot(page, url, page_config["name"])

                # Save screenshot locally
                screenshot_path = screenshots_dir / f"{i:02d}-{page_config['name'].lower()}.png"
                screenshot_path.write_bytes(screenshot_bytes)
                print(f"  Screenshot saved: {screenshot_path}")

                # Convert to base64 for Claude
                screenshot_base64 = base64.standard_b64encode(screenshot_bytes).decode("utf-8")

                # Analyze with Claude
                print(f"  Analyzing with Claude Vision...")
                result = analyze_screenshot_with_claude(
                    client,
                    screenshot_base64,
                    page_config["name"],
                    page_config["expected_elements"],
                )

                results.append(result)

                # Print analysis
                print()
                print(f"  Analysis for {page_config['name']}:")
                print("  " + "-" * 36)
                for line in result["analysis"].split("\n"):
                    print(f"  {line}")

            except Exception as e:
                print(f"  ERROR: {e}")
                results.append({
                    "page": page_config["name"],
                    "analysis": f"ERROR: Failed to verify - {e}",
                })

        await browser.close()

    # Summary
    print()
    print("=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    print()

    pass_count = 0
    warn_count = 0
    fail_count = 0

    for result in results:
        analysis = result["analysis"].upper()
        if "PASS" in analysis and "FAIL" not in analysis:
            status = "PASS"
            pass_count += 1
        elif "FAIL" in analysis:
            status = "FAIL"
            fail_count += 1
        elif "WARN" in analysis:
            status = "WARN"
            warn_count += 1
        else:
            status = "UNKNOWN"

        print(f"  [{status:4}] {result['page']}")

    print()
    print(f"Results: {pass_count} passed, {warn_count} warnings, {fail_count} failed")
    print(f"Screenshots saved in: {screenshots_dir.absolute()}")
    print()

    # Exit with appropriate code
    if fail_count > 0:
        sys.exit(1)
    elif warn_count > 0:
        sys.exit(0)
    else:
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
