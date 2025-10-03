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
from playwright.async_api import Page, BrowserContext


def sanitize_filename(url: str) -> str:
    """Create a safe filename from URL."""
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    path = parsed.path.strip("/").replace("/", "_")
    if path:
        return f"{domain}_{path}"
    return domain


class CrawlerEngine:
    def __init__(self):
        self.browser_config = BrowserConfig(
            headless=True,
            verbose=True
        )
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
            screenshot=True
        )

        async with AsyncWebCrawler(config=self.browser_config) as crawler:
            result = await crawler.arun(url, config=config)

            if result.success:
                print(f"✓ Page crawled successfully")

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

    async def _download_and_replace_resources(self, html: str, base_url: str,
                                            output_path: Path, css_dir: Path,
                                            js_dir: Path, images_dir: Path) -> str:
        """Download resources and modify HTML to use local paths with original filenames"""

        modified_html = html

        # Download CSS files
        css_resources = self._extract_css_resources(html, base_url)
        css_counter = 1
        for resource in css_resources:
            if resource["type"] == "external":
                # Keep original filename if available, otherwise use sequential naming
                original_filename = Path(urlparse(resource["full_url"]).path).name
                if not original_filename or not original_filename.endswith('.css'):
                    # Use sequential numbering instead of hash to ensure consistency
                    original_filename = f"style_{css_counter}.css"
                    css_counter += 1

                local_path = css_dir / original_filename

                success = await self._download_resource(resource["full_url"], local_path)
                if success:
                    # Replace in HTML using original href
                    old_href = resource["href"]
                    new_href = f"css/{original_filename}"
                    modified_html = modified_html.replace(f'href="{old_href}"', f'href="{new_href}"')
                    modified_html = modified_html.replace(f"href='{old_href}'", f"href='{new_href}'")
                    print(f"✓ Downloaded CSS: {original_filename}")

        # Download JS files
        js_resources = self._extract_js_resources(html, base_url)
        js_counter = 1
        for resource in js_resources:
            if resource["type"] == "external":
                # Keep original filename if available, otherwise use sequential naming
                original_filename = Path(urlparse(resource["full_url"]).path).name
                if not original_filename or not original_filename.endswith('.js'):
                    # Use sequential numbering instead of hash to ensure consistency
                    original_filename = f"script_{js_counter}.js"
                    js_counter += 1

                local_path = js_dir / original_filename

                success = await self._download_resource(resource["full_url"], local_path)
                if success:
                    # Replace in HTML using original src
                    old_src = resource["src"]
                    new_src = f"js/{original_filename}"
                    modified_html = modified_html.replace(f'src="{old_src}"', f'src="{new_src}"')
                    modified_html = modified_html.replace(f"src='{old_src}'", f"src='{new_src}'")
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

                success = await self._download_resource(resource["full_url"], local_path)
                if success:
                    # Replace in HTML using original src
                    old_src = resource["src"]
                    new_src = f"images/{original_filename}"
                    modified_html = modified_html.replace(f'src="{old_src}"', f'src="{new_src}"')
                    modified_html = modified_html.replace(f"src='{old_src}'", f"src='{new_src}'")
                    print(f"✓ Downloaded image: {original_filename}")

        return modified_html

    async def _download_resource(self, url: str, local_path: Path) -> bool:
        """Download a resource to local path"""
        try:
            # Skip if already downloaded
            if url in self.downloaded_resources:
                return self.downloaded_resources[url]

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
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
            from ..utils.image_compression import compress_base64_screenshot

            # Save with optimal compression (WebP 90% → JPEG 95% → PNG optimized)
            screenshot_path = output_path / "screenshot.png"
            success, message, file_size = compress_base64_screenshot(screenshot_base64, screenshot_path)
            
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
    
    async def _save_screenshot_fallback(self, screenshot_base64: str, output_path: Path):
        """Fallback method to save screenshot without compression"""
        try:
            import base64

            # Remove data URL prefix if present
            if screenshot_base64.startswith('data:image/png;base64,'):
                screenshot_base64 = screenshot_base64.replace('data:image/png;base64,', '')

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
                    "local_path": f"images/{Path(urlparse(img['full_url']).path).name}"
                }
                if img_entry not in media_info["images"]:
                    media_info["images"].append(img_entry)

        media_json_path = output_path / "media.json"
        async with aiofiles.open(media_json_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(media_info, indent=2))
        print(f"✓ Saved media info: {media_json_path} ({len(media_info.get('images', []))} images)")

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
                if href.startswith(('data:', '#', 'css/', './css/')):
                    continue
                full_url = urljoin(base_url, href)
                css_resources.append({
                    "href": href,
                    "full_url": full_url,
                    "type": "external"
                })

        # Find style tags with inline CSS
        style_pattern = r'<style[^>]*>(.*?)</style>'
        for i, match in enumerate(re.finditer(style_pattern, html, re.IGNORECASE | re.DOTALL)):
            css_content = match.group(1)
            css_resources.append({
                "content": css_content[:200] + "..." if len(css_content) > 200 else css_content,
                "type": "inline",
                "index": i
            })

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
            if src.startswith('data:') or src.startswith('#'):
                continue
            full_url = urljoin(base_url, src)
            js_resources.append({
                "src": src,
                "full_url": full_url,
                "type": "external"
            })

        # Find script tags with inline JS
        script_pattern = r'<script[^>]*>(.*?)</script>'
        for i, match in enumerate(re.finditer(script_pattern, html, re.IGNORECASE | re.DOTALL)):
            if 'src=' not in match.group(0):  # Only inline scripts
                js_content = match.group(1)
                js_resources.append({
                    "content": js_content[:200] + "..." if len(js_content) > 200 else js_content,
                    "type": "inline",
                    "index": i
                })

        return js_resources

    def _extract_image_resources(self, html: str, base_url: str) -> list:
        """Extract image resources from HTML"""
        image_resources = []

        # Find img tags
        img_pattern = r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>'
        for match in re.finditer(img_pattern, html, re.IGNORECASE):
            src = match.group(1)
            # Skip data URLs and already local URLs
            if src.startswith('data:') or src.startswith('#'):
                continue
            full_url = urljoin(base_url, src)
            image_resources.append({
                "src": src,
                "full_url": full_url,
                "type": "external"
            })

        return image_resources


async def save_assets(result, output_dir: Path, url: str):
    """Legacy function for backward compatibility"""
    engine = CrawlerEngine()
    name = sanitize_filename(url)
    await engine.save_assets(url, name, output_dir)


async def main(sites: list, output_dir: str, is_baseline: bool = False, use_date_structure: bool = None) -> bool:
    """Main function to crawl sites and save results with browser-style "Save As" format."""
    from ..config import settings
    
    # If use_date_structure is not explicitly set, use it for both baseline and current
    if use_date_structure is None:
        use_date_structure = True  # Always use date structure now
    
    if use_date_structure:
        # Create date-based directory using Dublin timezone
        date_str = settings.get_current_date()
        output_path = Path(output_dir) / date_str
        crawl_type = "baseline" if is_baseline else "current"
        print(f"Creating {crawl_type} snapshot for date: {date_str} (Dublin time)")
    else:
        output_path = Path(output_dir)
    
    output_path.mkdir(parents=True, exist_ok=True)

    engine = CrawlerEngine()
    all_success = True

    for site in sites:
        url = site['url']
        name = sanitize_filename(url)
        print(f"Starting crawl for {url}...")

        try:
            await engine.save_assets(url, name, output_path)
        except Exception as e:
            print(f"✗ Error crawling {url}: {e}")
            all_success = False

    return all_success


if __name__ == "__main__":
    # Example usage
    sites = [{"url": "https://python.org"}]
    asyncio.run(main(sites, "./output"))
