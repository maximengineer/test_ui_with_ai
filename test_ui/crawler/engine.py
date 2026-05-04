#!/usr/bin/env python3
"""
Crawler with browser-style "Save As" functionality using crawl4ai.
Downloads and localizes all CSS, JS, and image resources.
"""

import asyncio
import json
import re
import aiohttp
import aiofiles
from pathlib import Path
from urllib.parse import urljoin, urlparse
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode


# Phase A.3: canonical implementation moved to test_ui/common/url_id.py.
# This re-export keeps the crawler's historical `sanitize_filename` name
# importable for any caller that uses it (none in-repo today, but the symbol
# was public and removing it would be a silent breaking change).
from ..common.url_id import sanitize_filename  # noqa: E402,F401

# Phase B.3: per-site dir naming centralized in common/sites.py so the
# crawler and comparator can't drift on the convention. Re-exported here
# under the historical name for the call sites in main() below.
from ..common.sites import site_dir_name as _site_dir_name  # noqa: E402,F401


class CrawlerEngine:
    def __init__(self):
        self.browser_config = BrowserConfig(headless=True, verbose=True)
        self.downloaded_resources = {}  # Track downloaded resources to avoid duplicates

    async def save_assets(self, url: str, name: str, output_dir: Path):
        """
        Save website assets in browser "Save As" format:
        - index.html with local resource links
        - CSS files in css/ folder
        - JS files in js/ folder
        - Images in images/ folder
        """
        output_path = output_dir / name
        output_path.mkdir(parents=True, exist_ok=True)

        # Create resource directories
        css_dir = output_path / "css"
        js_dir = output_path / "js"
        images_dir = output_path / "images"
        css_dir.mkdir(exist_ok=True)
        js_dir.mkdir(exist_ok=True)
        images_dir.mkdir(exist_ok=True)

        print(f"Crawling {url} with resource downloading...")

        # Reset downloaded resources tracker
        self.downloaded_resources = {}

        # Configure crawler with resource detection
        config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            verbose=True,
            # Keep all resources for analysis
            exclude_all_images=False,
            exclude_external_images=False,
            exclude_external_links=False,
            exclude_social_media_links=False,
            # Wait for resources to load
            wait_for="body",
            delay_before_return_html=2.0,
            # Enable screenshot capture
            screenshot=True,
        )

        async with AsyncWebCrawler(config=self.browser_config) as crawler:
            result = await crawler.arun(url, config=config)

            if result.success:
                print("✓ Page crawled successfully")

                # Process and download resources
                modified_html = await self._download_and_replace_resources(
                    result.html, url, output_path, css_dir, js_dir, images_dir
                )

                # Save modified HTML
                html_path = output_path / "index.html"
                async with aiofiles.open(html_path, "w", encoding="utf-8") as f:
                    await f.write(modified_html)
                print(f"✓ Saved HTML: {html_path}")

                # Save screenshot if available
                if result.screenshot:
                    await self._save_screenshot(result.screenshot, output_path)

                # Save resource info
                await self._save_resource_info(result, output_path, url)

            else:
                print(f"✗ Failed to crawl {url}: {result.error_message}")

    async def _download_and_replace_resources(
        self,
        html: str,
        base_url: str,
        output_path: Path,
        css_dir: Path,
        js_dir: Path,
        images_dir: Path,
    ) -> str:
        """Download resources and modify HTML to use local paths with original filenames"""

        modified_html = html

        # Download CSS files
        css_resources = self._extract_css_resources(html, base_url)
        css_counter = 1
        for resource in css_resources:
            if resource["type"] == "external":
                # Keep original filename if available, otherwise use sequential naming
                original_filename = Path(urlparse(resource["full_url"]).path).name
                if not original_filename or not original_filename.endswith(".css"):
                    # Use sequential numbering instead of hash to ensure consistency
                    original_filename = f"style_{css_counter}.css"
                    css_counter += 1

                local_path = css_dir / original_filename

                success = await self._download_resource(
                    resource["full_url"], local_path
                )
                if success:
                    # Replace in HTML using original href
                    old_href = resource["href"]
                    new_href = f"css/{original_filename}"
                    modified_html = modified_html.replace(
                        f'href="{old_href}"', f'href="{new_href}"'
                    )
                    modified_html = modified_html.replace(
                        f"href='{old_href}'", f"href='{new_href}'"
                    )
                    print(f"✓ Downloaded CSS: {original_filename}")

        # Download JS files
        js_resources = self._extract_js_resources(html, base_url)
        js_counter = 1
        for resource in js_resources:
            if resource["type"] == "external":
                # Keep original filename if available, otherwise use sequential naming
                original_filename = Path(urlparse(resource["full_url"]).path).name
                if not original_filename or not original_filename.endswith(".js"):
                    # Use sequential numbering instead of hash to ensure consistency
                    original_filename = f"script_{js_counter}.js"
                    js_counter += 1

                local_path = js_dir / original_filename

                success = await self._download_resource(
                    resource["full_url"], local_path
                )
                if success:
                    # Replace in HTML using original src
                    old_src = resource["src"]
                    new_src = f"js/{original_filename}"
                    modified_html = modified_html.replace(
                        f'src="{old_src}"', f'src="{new_src}"'
                    )
                    modified_html = modified_html.replace(
                        f"src='{old_src}'", f"src='{new_src}'"
                    )
                    print(f"✓ Downloaded JS: {original_filename}")

        # Download images
        image_resources = self._extract_image_resources(html, base_url)
        image_counter = 1
        for resource in image_resources:
            if resource["type"] == "external":
                # Keep original filename with extension if available
                parsed_url = urlparse(resource["full_url"])
                original_filename = Path(parsed_url.path).name
                if not original_filename:
                    # Use sequential numbering with proper extension
                    ext = ".png"  # Default extension
                    if "jpg" in parsed_url.path or "jpeg" in parsed_url.path:
                        ext = ".jpg"
                    elif "gif" in parsed_url.path:
                        ext = ".gif"
                    elif "svg" in parsed_url.path:
                        ext = ".svg"
                    original_filename = f"image_{image_counter}{ext}"
                    image_counter += 1

                local_path = images_dir / original_filename

                success = await self._download_resource(
                    resource["full_url"], local_path
                )
                if success:
                    # Replace in HTML using original src
                    old_src = resource["src"]
                    new_src = f"images/{original_filename}"
                    modified_html = modified_html.replace(
                        f'src="{old_src}"', f'src="{new_src}"'
                    )
                    modified_html = modified_html.replace(
                        f"src='{old_src}'", f"src='{new_src}'"
                    )
                    print(f"✓ Downloaded image: {original_filename}")

        return modified_html

    async def _download_resource(self, url: str, local_path: Path) -> bool:
        """Download a resource to local path"""
        try:
            # Skip if already downloaded
            if url in self.downloaded_resources:
                return self.downloaded_resources[url]

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        content = await response.read()
                        async with aiofiles.open(local_path, "wb") as f:
                            await f.write(content)
                        self.downloaded_resources[url] = True
                        return True
                    else:
                        print(f"✗ Failed to download {url}: HTTP {response.status}")
                        self.downloaded_resources[url] = False
                        return False
        except Exception as e:
            print(f"✗ Error downloading {url}: {e}")
            self.downloaded_resources[url] = False
            return False

    async def _save_screenshot(self, screenshot_base64: str, output_path: Path):
        """Save screenshot from base64 data with compression optimization"""
        try:
            from ..common.images import compress_base64_screenshot

            # Save with optimal compression (WebP 90% → JPEG 95% → PNG optimized)
            screenshot_path = output_path / "screenshot.png"
            success, message, file_size = compress_base64_screenshot(
                screenshot_base64, screenshot_path
            )

            if success:
                print(f"✓ Saved compressed screenshot: {screenshot_path}")
                print(f"  {message}")
            else:
                print(f"✗ Screenshot compression failed: {message}")
                # Fallback to original method
                await self._save_screenshot_fallback(screenshot_base64, output_path)

        except Exception as e:
            print(f"✗ Error saving screenshot: {e}")
            # Fallback to original method
            await self._save_screenshot_fallback(screenshot_base64, output_path)

    async def _save_screenshot_fallback(
        self, screenshot_base64: str, output_path: Path
    ):
        """Fallback method to save screenshot without compression"""
        try:
            import base64

            # Remove data URL prefix if present
            if screenshot_base64.startswith("data:image/png;base64,"):
                screenshot_base64 = screenshot_base64.replace(
                    "data:image/png;base64,", ""
                )

            # Decode base64 to bytes
            screenshot_bytes = base64.b64decode(screenshot_base64)

            # Save as PNG file
            screenshot_path = output_path / "screenshot.png"
            async with aiofiles.open(screenshot_path, "wb") as f:
                await f.write(screenshot_bytes)

            print(f"✓ Saved screenshot (fallback): {screenshot_path}")

        except Exception as e:
            print(f"✗ Error saving screenshot (fallback): {e}")

    async def _save_resource_info(self, result, output_path: Path, base_url: str):
        """Save resource information for analysis"""

        # Save CSS info
        css_resources = self._extract_css_resources(result.html, base_url)
        css_json_path = output_path / "css.json"
        async with aiofiles.open(css_json_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(css_resources, indent=2))
        print(f"✓ Saved CSS info: {css_json_path} ({len(css_resources)} resources)")

        # Save JS info
        js_resources = self._extract_js_resources(result.html, base_url)
        js_json_path = output_path / "js.json"
        async with aiofiles.open(js_json_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(js_resources, indent=2))
        print(f"✓ Saved JS info: {js_json_path} ({len(js_resources)} resources)")

        # Combine crawl4ai media info with our extracted images
        media_info = result.media or {"images": [], "videos": [], "audios": []}

        # Add our extracted images
        image_resources = self._extract_image_resources(result.html, base_url)
        our_images = [img for img in image_resources if img["type"] == "external"]

        # Merge with crawl4ai's image detection
        if our_images:
            for img in our_images:
                # Add to media_info if not already there
                img_entry = {
                    "src": img["full_url"],
                    "alt": "",  # We could extract this from HTML if needed
                    "type": "image",
                    "local_path": f"images/{Path(urlparse(img['full_url']).path).name}",
                }
                if img_entry not in media_info["images"]:
                    media_info["images"].append(img_entry)

        media_json_path = output_path / "media.json"
        async with aiofiles.open(media_json_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(media_info, indent=2))
        print(
            f"✓ Saved media info: {media_json_path} ({len(media_info.get('images', []))} images)"
        )

    def _extract_css_resources(self, html: str, base_url: str) -> list:
        """Extract CSS resources from HTML with better pattern matching"""
        css_resources = []

        # Find link tags with CSS - improved pattern to catch more variations
        css_patterns = [
            r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
            r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']stylesheet["\'][^>]*>',
            r'<link[^>]*href=["\']([^"\']+\.css[^"\']*)["\'][^>]*>',
        ]

        for pattern in css_patterns:
            for match in re.finditer(pattern, html, re.IGNORECASE):
                href = match.group(1)
                # Skip data URLs, already local URLs, and fragments
                if href.startswith(("data:", "#", "css/", "./css/")):
                    continue
                full_url = urljoin(base_url, href)
                css_resources.append(
                    {"href": href, "full_url": full_url, "type": "external"}
                )

        # Find style tags with inline CSS
        style_pattern = r"<style[^>]*>(.*?)</style>"
        for i, match in enumerate(
            re.finditer(style_pattern, html, re.IGNORECASE | re.DOTALL)
        ):
            css_content = match.group(1)
            css_resources.append(
                {
                    "content": css_content[:200] + "..."
                    if len(css_content) > 200
                    else css_content,
                    "type": "inline",
                    "index": i,
                }
            )

        # Remove duplicates while preserving order
        seen_urls = set()
        unique_resources = []
        for resource in css_resources:
            if resource["type"] == "inline":
                unique_resources.append(resource)
            else:
                if resource["full_url"] not in seen_urls:
                    seen_urls.add(resource["full_url"])
                    unique_resources.append(resource)

        return unique_resources

    def _extract_js_resources(self, html: str, base_url: str) -> list:
        """Extract JavaScript resources from HTML"""
        js_resources = []

        # Find script tags with src
        js_link_pattern = r'<script[^>]*src=["\']([^"\']+)["\'][^>]*>'
        for match in re.finditer(js_link_pattern, html, re.IGNORECASE):
            src = match.group(1)
            # Skip data URLs and already local URLs
            if src.startswith("data:") or src.startswith("#"):
                continue
            full_url = urljoin(base_url, src)
            js_resources.append({"src": src, "full_url": full_url, "type": "external"})

        # Find script tags with inline JS
        script_pattern = r"<script[^>]*>(.*?)</script>"
        for i, match in enumerate(
            re.finditer(script_pattern, html, re.IGNORECASE | re.DOTALL)
        ):
            if "src=" not in match.group(0):  # Only inline scripts
                js_content = match.group(1)
                js_resources.append(
                    {
                        "content": js_content[:200] + "..."
                        if len(js_content) > 200
                        else js_content,
                        "type": "inline",
                        "index": i,
                    }
                )

        return js_resources

    def _extract_image_resources(self, html: str, base_url: str) -> list:
        """Extract image resources from HTML"""
        image_resources = []

        # Find img tags
        img_pattern = r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>'
        for match in re.finditer(img_pattern, html, re.IGNORECASE):
            src = match.group(1)
            # Skip data URLs and already local URLs
            if src.startswith("data:") or src.startswith("#"):
                continue
            full_url = urljoin(base_url, src)
            image_resources.append(
                {"src": src, "full_url": full_url, "type": "external"}
            )

        return image_resources


async def save_assets(result, output_dir: Path, url: str):
    """Legacy function for backward compatibility"""
    engine = CrawlerEngine()
    name = sanitize_filename(url)
    await engine.save_assets(url, name, output_dir)


async def main(
    sites: list,
    output_dir: str,
    is_baseline: bool = False,
    use_date_structure: bool = None,
    *,
    run_id: str | None = None,
) -> bool:
    """Crawl `sites`, publishing artifacts under `<output_dir>/<date>/<run_id>/`.

    Phase B.1: each crawl gets a fresh ULID `run_id` and writes into
    `<output_dir>/<date>/.tmp-<run_id>/<url_dir>/...` while in flight. On
    clean completion the directory is renamed to `<output_dir>/<date>/<run_id>/`
    (atomic on the same filesystem) and the `<output_dir>/<date>/latest`
    symlink is updated to point at it.

    A manifest.json is written at start (`status="running"`) and updated at
    the end (`status="complete"` plus file checksum + url_count).

    On any exception, the manifest is set to `status="failed"` and the
    `.tmp-<run_id>` directory is left in place for inspection - readers
    skip it because of the `.tmp-` prefix (per `find_latest_run_dir_in_date`).

    The `use_date_structure` parameter is retained for backward compatibility
    with old callers but is now effectively always True.
    """
    from ..common.preconditions import require_no_live_lock
    from ..common.run_context import run_context
    from ..common.run_id import is_valid_run_id, new_run_id
    from ..common.run_record import write_run_record
    from ..comparator.finder import update_latest_symlink
    from ..config import settings

    if use_date_structure is False:
        # Pre-A.0 escape hatch - write directly to output_dir, no run_id, no
        # manifest. Kept for any test harness that pre-dates the date layout.
        # New code paths always take the date+run_id branch below.
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        engine = CrawlerEngine()
        all_success = True
        for site in sites:
            url = site["url"]
            name = _site_dir_name(site)
            print(f"Starting crawl for {url}...")
            try:
                await engine.save_assets(url, name, output_path)
            except Exception as e:
                print(f"✗ Error crawling {url}: {e}")
                all_success = False
        return all_success

    date_str = settings.get_current_date()
    date_dir = Path(output_dir) / date_str
    date_dir.mkdir(parents=True, exist_ok=True)

    kind = "baseline" if is_baseline else "current"

    # Refuse to start if a sibling .tmp- run for the same date holds a live
    # lock - prevents two concurrent crawls clobbering shared state. Stale
    # locks (dead PGID) are silently ignored so a previous SIGKILL doesn't
    # block forever.
    # Validate run_id BEFORE acquiring the lock - a bogus run_id from a
    # misconfigured caller should fail fast, not after up to a 5s lock
    # wait. Comparator engine validates in the same position; aligning here.
    if run_id is None:
        run_id = new_run_id()
    elif not is_valid_run_id(run_id):
        raise ValueError(f"run_id={run_id!r} is not a valid ULID")

    require_no_live_lock(date_dir, kind_label=kind)

    print(f"Creating {kind} snapshot run {run_id} for date {date_str} (Dublin time)")

    # Phase B.3.4: persist a run-invocation record outside the run dir so a
    # future dashboard / retry tool knows the parameters this was started
    # with. Best-effort - failure logged, doesn't abort the crawl.
    write_run_record(
        run_id,
        kind=kind,
        args={
            "output_dir": str(output_dir),
            "is_baseline": is_baseline,
            "site_count": len(sites),
        },
    )

    engine = CrawlerEngine()

    with run_context(
        date_dir,
        run_id,
        kind=kind,
        command=f"crawler.main is_baseline={is_baseline}",
    ) as ctx:
        all_success = True
        crawled_count = 0
        for site in sites:
            url = site["url"]
            name = _site_dir_name(site)
            print(f"Starting crawl for {url}...")
            try:
                await engine.save_assets(url, name, ctx.run_root)
                crawled_count += 1
            except Exception as e:
                print(f"✗ Error crawling {url}: {e}")
                all_success = False
        ctx.complete(url_count=crawled_count)

    # `latest` symlink update happens AFTER the rename so it points at the
    # final path. Symlink failure is non-fatal - readers fall back to
    # scanning ULID dirs.
    final_run_dir = date_dir / run_id
    try:
        update_latest_symlink(date_dir, run_id)
    except Exception as e:
        print(f"⚠ Warning: could not update 'latest' symlink: {e}")

    print(f"✓ {kind} run {run_id} published to {final_run_dir}")
    return all_success


if __name__ == "__main__":
    # Example usage
    sites = [{"url": "https://python.org"}]
    asyncio.run(main(sites, "./output"))
