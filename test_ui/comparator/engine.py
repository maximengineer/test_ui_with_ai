"""Comparator engine for purely technical comparison without AI analysis."""
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from loguru import logger
from bs4 import BeautifulSoup
from ..config import settings

# Import these only when needed to avoid Docker build issues
try:
    from skimage.metrics import structural_similarity as ssim
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class ComparatorEngine:
    """Handles purely technical comparison between baseline and current snapshots."""

    def __init__(self):
        pass
        
    @classmethod
    def find_latest_baseline(cls, baseline_root: Path) -> Optional[Path]:
        """Find the latest baseline directory by date (dd-mm-yyyy format)."""
        return cls._find_latest_date_dir(baseline_root)
        
    @classmethod
    def find_latest_current(cls, current_root: Path) -> Optional[Path]:
        """Find the latest current directory by date (dd-mm-yyyy format)."""
        return cls._find_latest_date_dir(current_root)
    
    @classmethod
    def _find_latest_date_dir(cls, root_path: Path) -> Optional[Path]:
        """Find the latest date directory in the given root path."""
        if not root_path.exists():
            return None
            
        # Look for date directories in dd-mm-yyyy format
        date_dirs = []
        for item in root_path.iterdir():
            if item.is_dir() and cls._is_valid_date_dir(item.name):
                date_dirs.append(item)
                
        if not date_dirs:
            return None
            
        # Sort by date (newest first)
        date_dirs.sort(key=lambda x: cls._parse_date_dir(x.name), reverse=True)
        return date_dirs[0]
        
    @staticmethod
    def _is_valid_date_dir(dirname: str) -> bool:
        """Check if directory name matches dd-mm-yyyy pattern."""
        try:
            parts = dirname.split('-')
            if len(parts) != 3:
                return False
            day, month, year = parts
            return (len(day) == 2 and day.isdigit() and 
                   len(month) == 2 and month.isdigit() and 
                   len(year) == 4 and year.isdigit() and
                   1 <= int(day) <= 31 and 1 <= int(month) <= 12)
        except (ValueError, AttributeError):
            return False
            
    @staticmethod  
    def _parse_date_dir(dirname: str) -> datetime:
        """Parse date directory name to datetime object."""
        day, month, year = dirname.split('-')
        return datetime(int(year), int(month), int(day))

    def compare_all(self, baseline_dir: Path, current_dir: Path, urls: List[str]) -> List[Dict[str, Any]]:
        """Compare all snapshots and generate per-URL structured JSON output."""
        date_str = settings.get_current_date()
        logger.info(f"Starting comparison for date: {date_str} (Dublin time)")
        
        baseline_sites = {d.name for d in baseline_dir.iterdir() if d.is_dir()}
        current_sites = {d.name for d in current_dir.iterdir() if d.is_dir()}
        
        all_results = []
        
        for url in urls:
            # Convert URL to directory name (same logic as crawler)
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            path = parsed.path.strip("/").replace("/", "_")
            url_dir_name = f"{domain}_{path}" if path else domain
            
            # Create per-URL output directory: data/comparator/{date}/{url_dir_name}/
            url_output_dir = Path("data/comparator") / date_str / url_dir_name
            url_output_dir.mkdir(parents=True, exist_ok=True)
            
            # Diffs directory will be created only if there are actual differences
            diffs_dir = url_output_dir / "diffs"
            
            logger.info(f"Comparing URL: {url} -> {url_output_dir}")
            
            # Find the corresponding site directory in baseline/current
            # Use the same sanitize_filename logic as the crawler
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            path = parsed.path.strip("/").replace("/", "_")
            site_name = f"{domain}_{path}" if path else domain
            baseline_path = baseline_dir / site_name
            current_path = current_dir / site_name
            
            if site_name not in baseline_sites:
                logger.warning(f"Site '{site_name}' not found in baseline")
                result = {
                    "url": url,
                    "error": "missing_baseline", 
                    "message": "Site not found in baseline"
                }
            elif site_name not in current_sites:
                logger.warning(f"Site '{site_name}' not found in current crawl")
                result = {
                    "url": url,
                    "error": "missing_current", 
                    "message": "Site not found in current crawl"
                }
            else:
                # Perform comparison for this URL
                result = self._compare_single_site(
                    baseline_path=baseline_path,
                    current_path=current_path,
                    url=url,
                    diffs_dir=diffs_dir
                )
                result["url"] = url
            
            # Create individual comparison data for this URL
            comparison_data = {
                "metadata": {
                    "timestamp": settings.get_current_datetime(),
                    "url": url,
                    "baseline_path": str(baseline_dir.absolute()),
                    "current_path": str(current_dir.absolute()),
                    "output_path": str(url_output_dir.absolute())
                },
                "result": result
            }
            
            # Save individual JSON file for this URL
            output_file = url_output_dir / "comparison_results.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(comparison_data, f, indent=2, ensure_ascii=False)
                
            logger.info(f"Comparison results for {url} saved to: {output_file}")
            all_results.append(comparison_data)
        
        logger.info(f"All comparisons complete - {len(all_results)} URLs processed")
        return all_results

    def _compare_assets(self, baseline_dir: Path, current_dir: Path, asset_type: str) -> Dict[str, Any]:
        """Compare asset directories with detailed content analysis for AI."""
        baseline_asset_dir = baseline_dir / asset_type
        current_asset_dir = current_dir / asset_type
        
        if not baseline_asset_dir.exists() and not current_asset_dir.exists():
            return {
                "added": [], "removed": [], "changed": [], "has_changes": False,
                "total_changes": 0, "content_changes": [], "detailed_analysis": {}
            }
            
        baseline_files = set()
        current_files = set()
        
        if baseline_asset_dir.exists():
            baseline_files = {p.name for p in baseline_asset_dir.glob('*') if p.is_file()}
            
        if current_asset_dir.exists():
            current_files = {p.name for p in current_asset_dir.glob('*') if p.is_file()}
            
        added = sorted(list(current_files - baseline_files))
        removed = sorted(list(baseline_files - current_files))
        common = baseline_files.intersection(current_files)
        
        # Enhanced content analysis for changed files
        changed = []
        content_changes = []
        detailed_analysis = {}
        
        for filename in common:
            baseline_file = baseline_asset_dir / filename
            current_file = current_asset_dir / filename
            
            if baseline_file.exists() and current_file.exists():
                # Detailed content comparison
                if asset_type == "css":
                    content_analysis = self._analyze_css_content_changes(baseline_file, current_file, filename)
                elif asset_type == "js":
                    content_analysis = self._analyze_js_content_changes(baseline_file, current_file, filename)
                else:
                    # For media files, just check hash
                    baseline_hash = hashlib.md5(baseline_file.read_bytes()).hexdigest()
                    current_hash = hashlib.md5(current_file.read_bytes()).hexdigest()
                    content_analysis = {
                        "has_changes": baseline_hash != current_hash,
                        "changes": [],
                        "analysis": {"file_changed": baseline_hash != current_hash}
                    }
                
                if content_analysis["has_changes"]:
                    changed.append(filename)
                    content_changes.extend(content_analysis["changes"])
                    detailed_analysis[filename] = content_analysis["analysis"]
        
        # Add detailed info for added/removed files
        for filename in added:
            file_path = current_asset_dir / filename
            detailed_analysis[filename] = self._analyze_new_file(file_path, asset_type, "added")
            
        for filename in removed:
            file_path = baseline_asset_dir / filename
            detailed_analysis[filename] = self._analyze_new_file(file_path, asset_type, "removed")
                    
        has_changes = len(added) > 0 or len(removed) > 0 or len(changed) > 0
        
        return {
            "added": added,
            "removed": removed, 
            "changed": sorted(changed),
            "has_changes": has_changes,
            "total_changes": len(added) + len(removed) + len(changed),
            "content_changes": content_changes,
            "detailed_analysis": detailed_analysis
        }

    def _compare_dom(self, baseline_html: Path, current_html: Path) -> Dict[str, Any]:
        """Compare DOM structure and extract detailed metrics for AI analysis."""
        if not baseline_html.exists() or not current_html.exists():
            return {"error": "HTML files missing"}
            
        try:
            with open(baseline_html, 'r', encoding='utf-8') as f:
                baseline_soup = BeautifulSoup(f.read(), 'lxml')
            with open(current_html, 'r', encoding='utf-8') as f:
                current_soup = BeautifulSoup(f.read(), 'lxml')
                
            # Extract clean title text
            baseline_title = baseline_soup.find('title')
            current_title = current_soup.find('title')
            
            # Clean whitespace and normalize title text
            baseline_title_text = baseline_title.get_text(strip=True) if baseline_title else ""
            current_title_text = current_title.get_text(strip=True) if current_title else ""
            
            # Enhanced tag analysis for AI with detailed element inspection
            tag_types = ['img', 'a', 'script', 'link', 'form', 'button', 'input', 'div', 'span', 'p', 'h1', 'h2', 'h3', 'nav', 'header', 'footer', 'section', 'article']
            baseline_counts = {tag: len(baseline_soup.find_all(tag)) for tag in tag_types}
            current_counts = {tag: len(current_soup.find_all(tag)) for tag in tag_types}
            
            # Deep element analysis with code snippets
            element_changes = []
            specific_element_changes = []
            
            for tag in tag_types:
                baseline_count = baseline_counts[tag]
                current_count = current_counts[tag]
                if baseline_count != current_count:
                    change_type = "added" if current_count > baseline_count else "removed"
                    count_diff = abs(current_count - baseline_count)
                    
                    # Get specific elements that changed
                    baseline_elements = baseline_soup.find_all(tag)
                    current_elements = current_soup.find_all(tag)
                    
                    # Extract code snippets of changed elements
                    code_examples = self._extract_element_code_snippets(
                        baseline_elements, current_elements, tag, change_type
                    )
                    
                    element_changes.append({
                        "element": tag,
                        "change_type": change_type,
                        "count_change": count_diff,
                        "baseline_count": baseline_count,
                        "current_count": current_count,
                        "impact": self._assess_element_impact(tag, change_type, count_diff),
                        "code_examples": code_examples
                    })
                    
                    # Add to specific changes for detailed reporting
                    specific_element_changes.extend(code_examples)
            
            # Content analysis
            baseline_text = baseline_soup.get_text(strip=True)
            current_text = current_soup.get_text(strip=True)
            content_length_change = len(current_text) - len(baseline_text)
            
            # Meta information analysis
            baseline_meta = self._extract_meta_info(baseline_soup)
            current_meta = self._extract_meta_info(current_soup)
            meta_changes = self._compare_meta_info(baseline_meta, current_meta)
            
            # Navigation structure analysis
            nav_changes = self._analyze_navigation_changes(baseline_soup, current_soup)
            
            # Determine change categories
            title_changed = baseline_title_text != current_title_text
            content_changed = abs(content_length_change) > 50  # Significant content change
            structure_changed = len(element_changes) > 0
            meta_changed = len(meta_changes) > 0
            nav_changed = len(nav_changes) > 0
            
            has_changes = title_changed or content_changed or structure_changed or meta_changed or nav_changed
            
            return {
                "title": {
                    "changed": title_changed,
                    "baseline": baseline_title_text,
                    "current": current_title_text
                },
                "structure": {
                    "element_changes": element_changes,
                    "specific_changes": specific_element_changes,
                    "tag_counts": {
                        "baseline": baseline_counts,
                        "current": current_counts
                    }
                },
                "content": {
                    "baseline_length": len(baseline_text),
                    "current_length": len(current_text),
                    "length_change": content_length_change,
                    "significant_change": abs(content_length_change) > 100
                },
                "meta": {
                    "changes": meta_changes
                },
                "navigation": {
                    "changes": nav_changes
                },
                "has_changes": has_changes
            }
            
        except Exception as e:
            logger.error(f"Error comparing DOM: {e}")
            return {"error": f"DOM comparison failed: {str(e)}"}

    def _assess_element_impact(self, tag: str, change_type: str, count_diff: int) -> str:
        """Assess the impact of element changes for AI analysis."""
        # High impact elements
        high_impact_tags = ['form', 'button', 'input', 'nav', 'header', 'footer']
        # Medium impact elements  
        medium_impact_tags = ['a', 'img', 'h1', 'h2', 'h3', 'section', 'article']
        # Low impact elements
        low_impact_tags = ['div', 'span', 'p']
        
        if tag in high_impact_tags:
            return "high" if count_diff > 0 else "medium"
        elif tag in medium_impact_tags:
            return "medium" if count_diff > 2 else "low"
        elif tag in low_impact_tags:
            return "low" if count_diff < 10 else "medium"
        else:
            return "low"

    def _extract_meta_info(self, soup) -> Dict[str, str]:
        """Extract meta information from HTML."""
        meta_info = {}
        
        # Extract important meta tags
        meta_tags = soup.find_all('meta')
        for meta in meta_tags:
            name = meta.get('name') or meta.get('property')
            content = meta.get('content')
            if name and content:
                meta_info[name] = content
        
        # Extract description and keywords specifically
        description = soup.find('meta', attrs={'name': 'description'})
        if description:
            meta_info['description'] = description.get('content', '')
            
        keywords = soup.find('meta', attrs={'name': 'keywords'})
        if keywords:
            meta_info['keywords'] = keywords.get('content', '')
            
        return meta_info

    def _compare_meta_info(self, baseline_meta: Dict[str, str], current_meta: Dict[str, str]) -> list:
        """Compare meta information and return changes."""
        changes = []
        
        # Check for added meta tags
        for key in current_meta:
            if key not in baseline_meta:
                changes.append({
                    "type": "meta_added",
                    "key": key,
                    "new_value": current_meta[key],
                    "impact": "low" if key not in ['description', 'title', 'keywords'] else "medium"
                })
        
        # Check for removed meta tags
        for key in baseline_meta:
            if key not in current_meta:
                changes.append({
                    "type": "meta_removed",
                    "key": key,
                    "old_value": baseline_meta[key],
                    "impact": "low" if key not in ['description', 'title', 'keywords'] else "medium"
                })
        
        # Check for changed meta tags
        for key in baseline_meta:
            if key in current_meta and baseline_meta[key] != current_meta[key]:
                changes.append({
                    "type": "meta_changed",
                    "key": key,
                    "old_value": baseline_meta[key],
                    "new_value": current_meta[key],
                    "impact": "high" if key in ['description', 'title'] else "medium"
                })
        
        return changes

    def _analyze_navigation_changes(self, baseline_soup, current_soup) -> list:
        """Analyze navigation structure changes."""
        changes = []
        
        # Find navigation elements
        baseline_navs = baseline_soup.find_all(['nav', 'menu']) + baseline_soup.find_all(class_=lambda x: x and 'nav' in x.lower())
        current_navs = current_soup.find_all(['nav', 'menu']) + current_soup.find_all(class_=lambda x: x and 'nav' in x.lower())
        
        # Analyze navigation link counts
        baseline_nav_links = []
        for nav in baseline_navs:
            links = nav.find_all('a')
            baseline_nav_links.extend([link.get_text(strip=True) for link in links])
            
        current_nav_links = []
        for nav in current_navs:
            links = nav.find_all('a') 
            current_nav_links.extend([link.get_text(strip=True) for link in links])
        
        # Compare navigation links
        if len(baseline_nav_links) != len(current_nav_links):
            changes.append({
                "type": "navigation_count_change",
                "baseline_count": len(baseline_nav_links),
                "current_count": len(current_nav_links),
                "impact": "medium"
            })
        
        # Check for new/removed navigation items
        baseline_set = set(baseline_nav_links)
        current_set = set(current_nav_links)
        
        new_items = current_set - baseline_set
        removed_items = baseline_set - current_set
        
        for item in new_items:
            changes.append({
                "type": "navigation_item_added",
                "item": item,
                "impact": "medium"
            })
        
        for item in removed_items:
            changes.append({
                "type": "navigation_item_removed", 
                "item": item,
                "impact": "high"
            })
        
        return changes

    def _analyze_css_content_changes(self, baseline_file: Path, current_file: Path, filename: str) -> Dict[str, Any]:
        """Analyze specific CSS content changes for AI analysis."""
        try:
            with open(baseline_file, 'r', encoding='utf-8') as f:
                baseline_content = f.read()
            with open(current_file, 'r', encoding='utf-8') as f:
                current_content = f.read()
            
            if baseline_content == current_content:
                return {"has_changes": False, "changes": [], "analysis": {}}
            
            # Parse CSS rules for detailed analysis
            baseline_rules = self._parse_css_rules(baseline_content)
            current_rules = self._parse_css_rules(current_content)
            
            changes = []
            
            # Check for added selectors
            for selector in current_rules:
                if selector not in baseline_rules:
                    changes.append({
                        "type": "css_selector_added",
                        "file": filename,
                        "selector": selector,
                        "properties": current_rules[selector],
                        "impact": self._assess_css_impact(selector, current_rules[selector]),
                        "code_snippet": f"{selector} {{\n  {'; '.join(f'{k}: {v}' for k, v in current_rules[selector].items())}\n}}"
                    })
            
            # Check for removed selectors
            for selector in baseline_rules:
                if selector not in current_rules:
                    changes.append({
                        "type": "css_selector_removed", 
                        "file": filename,
                        "selector": selector,
                        "properties": baseline_rules[selector],
                        "impact": self._assess_css_impact(selector, baseline_rules[selector]),
                        "code_snippet": f"{selector} {{\n  {'; '.join(f'{k}: {v}' for k, v in baseline_rules[selector].items())}\n}}"
                    })
            
            # Check for modified selectors
            for selector in baseline_rules:
                if selector in current_rules:
                    baseline_props = baseline_rules[selector]
                    current_props = current_rules[selector]
                    
                    if baseline_props != current_props:
                        prop_changes = []
                        for prop in set(baseline_props.keys()) | set(current_props.keys()):
                            old_val = baseline_props.get(prop)
                            new_val = current_props.get(prop)
                            if old_val != new_val:
                                prop_changes.append({
                                    "property": prop,
                                    "old_value": old_val,
                                    "new_value": new_val
                                })
                        
                        changes.append({
                            "type": "css_selector_modified",
                            "file": filename, 
                            "selector": selector,
                            "property_changes": prop_changes,
                            "impact": self._assess_css_impact(selector, current_props),
                            "code_snippet": f"{selector} {{\n  {'; '.join(f'{k}: {v}' for k, v in current_props.items())}\n}}"
                        })
            
            return {
                "has_changes": True,
                "changes": changes,
                "analysis": {
                    "total_selectors_baseline": len(baseline_rules),
                    "total_selectors_current": len(current_rules),
                    "added_selectors": len([c for c in changes if c["type"] == "css_selector_added"]),
                    "removed_selectors": len([c for c in changes if c["type"] == "css_selector_removed"]),
                    "modified_selectors": len([c for c in changes if c["type"] == "css_selector_modified"])
                }
            }
            
        except Exception as e:
            logger.error(f"Error analyzing CSS content for {filename}: {e}")
            return {"has_changes": False, "changes": [], "analysis": {"error": str(e)}}

    def _analyze_js_content_changes(self, baseline_file: Path, current_file: Path, filename: str) -> Dict[str, Any]:
        """Analyze specific JS content changes for AI analysis."""
        try:
            with open(baseline_file, 'r', encoding='utf-8') as f:
                baseline_content = f.read()
            with open(current_file, 'r', encoding='utf-8') as f:
                current_content = f.read()
            
            if baseline_content == current_content:
                return {"has_changes": False, "changes": [], "analysis": {}}
            
            # Simple function/variable detection for JS analysis
            baseline_functions = self._extract_js_functions(baseline_content)
            current_functions = self._extract_js_functions(current_content)
            
            baseline_vars = self._extract_js_variables(baseline_content)
            current_vars = self._extract_js_variables(current_content)
            
            changes = []
            
            # Function changes
            for func_name in current_functions:
                if func_name not in baseline_functions:
                    changes.append({
                        "type": "js_function_added",
                        "file": filename,
                        "function_name": func_name,
                        "code_snippet": current_functions[func_name][:200] + "..." if len(current_functions[func_name]) > 200 else current_functions[func_name],
                        "impact": "high"
                    })
            
            for func_name in baseline_functions:
                if func_name not in current_functions:
                    changes.append({
                        "type": "js_function_removed",
                        "file": filename,
                        "function_name": func_name,
                        "code_snippet": baseline_functions[func_name][:200] + "..." if len(baseline_functions[func_name]) > 200 else baseline_functions[func_name],
                        "impact": "high"
                    })
            
            for func_name in baseline_functions:
                if func_name in current_functions and baseline_functions[func_name] != current_functions[func_name]:
                    changes.append({
                        "type": "js_function_modified",
                        "file": filename,
                        "function_name": func_name,
                        "code_snippet": current_functions[func_name][:200] + "..." if len(current_functions[func_name]) > 200 else current_functions[func_name],
                        "impact": "high"
                    })
            
            # Variable changes (simplified)
            var_changes = []
            if len(baseline_vars) != len(current_vars):
                var_changes.append({
                    "type": "js_variables_count_changed",
                    "baseline_count": len(baseline_vars),
                    "current_count": len(current_vars),
                    "impact": "medium"
                })
            
            changes.extend(var_changes)
            
            return {
                "has_changes": True,
                "changes": changes,
                "analysis": {
                    "functions_baseline": len(baseline_functions),
                    "functions_current": len(current_functions),
                    "variables_baseline": len(baseline_vars),
                    "variables_current": len(current_vars),
                    "content_length_change": len(current_content) - len(baseline_content)
                }
            }
            
        except Exception as e:
            logger.error(f"Error analyzing JS content for {filename}: {e}")
            return {"has_changes": False, "changes": [], "analysis": {"error": str(e)}}

    def _analyze_new_file(self, file_path: Path, asset_type: str, change_type: str) -> Dict[str, Any]:
        """Analyze a new (added/removed) file for AI context."""
        try:
            if not file_path.exists():
                return {"error": "File not found", "change_type": change_type}
                
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            analysis = {
                "change_type": change_type,
                "file_size": len(content),
                "line_count": len(content.splitlines())
            }
            
            if asset_type == "css":
                rules = self._parse_css_rules(content)
                analysis.update({
                    "selector_count": len(rules),
                    "sample_selectors": list(rules.keys())[:5],
                    "code_snippet": content[:300] + "..." if len(content) > 300 else content
                })
            elif asset_type == "js":
                functions = self._extract_js_functions(content)
                analysis.update({
                    "function_count": len(functions),
                    "sample_functions": list(functions.keys())[:5],
                    "code_snippet": content[:300] + "..." if len(content) > 300 else content
                })
            
            return analysis
            
        except Exception as e:
            return {"error": str(e), "change_type": change_type}

    def _parse_css_rules(self, css_content: str) -> Dict[str, Dict[str, str]]:
        """Simple CSS rule extraction for analysis."""
        import re
        rules = {}
        
        # Basic CSS rule parsing (simplified)
        pattern = r'([^{}]+)\s*{\s*([^{}]+)\s*}'
        matches = re.findall(pattern, css_content)
        
        for selector, properties in matches:
            selector = selector.strip()
            props = {}
            
            # Parse properties
            for prop in properties.split(';'):
                if ':' in prop:
                    key, value = prop.split(':', 1)
                    props[key.strip()] = value.strip()
            
            if props:  # Only add if has properties
                rules[selector] = props
        
        return rules

    def _extract_js_functions(self, js_content: str) -> Dict[str, str]:
        """Extract JavaScript functions for analysis."""
        import re
        functions = {}
        
        # Function declaration patterns
        patterns = [
            r'function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}',
            r'const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}',
            r'([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, js_content, re.DOTALL)
            for match in matches:
                if len(match) >= 2:
                    func_name, func_body = match[0], match[1]
                    functions[func_name] = f"function {func_name}() {{{func_body}}}"
        
        return functions

    def _extract_js_variables(self, js_content: str) -> Dict[str, str]:
        """Extract JavaScript variables for analysis."""
        import re
        variables = {}
        
        # Variable declaration patterns
        patterns = [
            r'var\s+([a-zA-Z_$][a-zA-Z0-9_$]*)',
            r'let\s+([a-zA-Z_$][a-zA-Z0-9_$]*)',
            r'const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, js_content)
            for var_name in matches:
                variables[var_name] = "declared"
        
        return variables

    def _assess_css_impact(self, selector: str, properties: Dict[str, str]) -> str:
        """Assess the impact of CSS changes for AI analysis."""
        # Layout-affecting properties
        layout_props = ['width', 'height', 'margin', 'padding', 'display', 'position', 'float', 'flex', 'grid']
        # High-impact selectors
        high_impact_selectors = ['body', 'html', '.header', '.footer', '.nav', '.main']
        
        if any(prop in properties for prop in layout_props):
            return "high"
        elif any(selector.startswith(sel) for sel in high_impact_selectors):
            return "medium"
        else:
            return "low"

    def _extract_element_code_snippets(self, baseline_elements, current_elements, tag: str, change_type: str) -> list:
        """Extract code snippets of changed elements for AI analysis."""
        code_examples = []
        
        try:
            # Convert to string representations for comparison
            baseline_strings = [str(elem) for elem in baseline_elements]
            current_strings = [str(elem) for elem in current_elements]
            
            if change_type == "added":
                # Find elements in current that aren't in baseline
                new_elements = []
                for current_elem_str in current_strings:
                    if current_elem_str not in baseline_strings:
                        new_elements.append(current_elem_str)
                
                # Limit to first 3 examples to avoid overwhelming data
                for i, elem_str in enumerate(new_elements[:3]):
                    code_examples.append({
                        "change_type": "added",
                        "element": tag,
                        "code_snippet": self._clean_html_snippet(elem_str),
                        "description": f"New {tag} element added",
                        "position": f"example_{i+1}",
                        "impact": self._assess_element_impact(tag, change_type, 1)
                    })
                    
            elif change_type == "removed":
                # Find elements in baseline that aren't in current
                removed_elements = []
                for baseline_elem_str in baseline_strings:
                    if baseline_elem_str not in current_strings:
                        removed_elements.append(baseline_elem_str)
                
                # Limit to first 3 examples
                for i, elem_str in enumerate(removed_elements[:3]):
                    code_examples.append({
                        "change_type": "removed",
                        "element": tag,
                        "code_snippet": self._clean_html_snippet(elem_str),
                        "description": f"{tag} element removed",
                        "position": f"example_{i+1}",
                        "impact": self._assess_element_impact(tag, change_type, 1)
                    })
            
            return code_examples
            
        except Exception as e:
            logger.error(f"Error extracting code snippets for {tag}: {e}")
            return [{
                "change_type": change_type,
                "element": tag,
                "code_snippet": f"Error extracting snippet: {str(e)}",
                "description": f"{tag} element {change_type}",
                "impact": "low"
            }]

    def _clean_html_snippet(self, html_string: str) -> str:
        """Clean and truncate HTML snippet for AI analysis."""
        # Remove excessive whitespace
        import re
        cleaned = re.sub(r'\s+', ' ', html_string.strip())
        
        # Truncate if too long, but preserve structure
        if len(cleaned) > 300:
            # Try to find a good break point
            if '>' in cleaned[200:300]:
                break_point = cleaned.find('>', 200) + 1
                cleaned = cleaned[:break_point] + "..."
            else:
                cleaned = cleaned[:300] + "..."
        
        return cleaned

    def _create_html_changes_json(self, dom_result: Dict[str, Any]) -> Dict[str, Any]:
        """Create structured HTML changes JSON for AI analysis following SMART_DIFFS_PLAN.md."""
        if "error" in dom_result:
            return {
                "changes_detected": False,
                "change_types": [],
                "changes": [],
                "summary": {
                    "total_changes": 0,
                    "structural_changes": 0,
                    "content_changes": 0,
                    "meta_changes": 0,
                    "navigation_changes": 0,
                    "severity": "none"
                }
            }
        
        changes = []
        change_types = []
        
        # Title changes
        title_info = dom_result.get("title", {})
        if title_info.get("changed", False):
            changes.append({
                "type": "content",
                "element": "title",
                "change": "text_modified",
                "description": "Page title changed",
                "old_value": title_info.get("baseline", ""),
                "new_value": title_info.get("current", ""),
                "impact": "medium"
            })
            change_types.append("content")
        
        # Enhanced element changes analysis with code snippets
        structure_info = dom_result.get("structure", {})
        element_changes = structure_info.get("element_changes", [])
        specific_changes = structure_info.get("specific_changes", [])
        
        for elem_change in element_changes:
            # Add summary change
            changes.append({
                "type": "structure",
                "element": elem_change["element"],
                "change": f"{elem_change['change_type']}_element",
                "description": f"{elem_change['change_type'].title()} {elem_change['count_change']} {elem_change['element']} element(s)",
                "old_value": elem_change["baseline_count"],
                "new_value": elem_change["current_count"],
                "impact": elem_change["impact"],
                "code_examples_count": len(elem_change.get("code_examples", []))
            })
            if "structure" not in change_types:
                change_types.append("structure")
        
        # Add specific element changes with code snippets
        for specific_change in specific_changes:
            changes.append({
                "type": "structure_detail",
                "element": specific_change["element"],
                "change": specific_change["change_type"],
                "description": specific_change["description"],
                "code_snippet": specific_change["code_snippet"],
                "position": specific_change.get("position", ""),
                "impact": specific_change["impact"]
            })
        
        # Meta information changes
        meta_info = dom_result.get("meta", {})
        meta_changes = meta_info.get("changes", [])
        for meta_change in meta_changes:
            changes.append({
                "type": "attributes",
                "element": f"meta[{meta_change['key']}]",
                "change": meta_change["type"],
                "description": f"Meta tag '{meta_change['key']}' {meta_change['type'].replace('meta_', '')}",
                "old_value": meta_change.get("old_value", ""),
                "new_value": meta_change.get("new_value", ""),
                "impact": meta_change["impact"]
            })
            if "attributes" not in change_types:
                change_types.append("attributes")
        
        # Navigation changes
        nav_info = dom_result.get("navigation", {})
        nav_changes = nav_info.get("changes", [])
        for nav_change in nav_changes:
            changes.append({
                "type": "structure",
                "element": "nav",
                "change": nav_change["type"],
                "description": f"Navigation {nav_change['type'].replace('navigation_', '').replace('_', ' ')}",
                "old_value": nav_change.get("baseline_count", nav_change.get("item", "")),
                "new_value": nav_change.get("current_count", ""),
                "impact": nav_change["impact"]
            })
            if "structure" not in change_types:
                change_types.append("structure")
        
        # Content changes
        content_info = dom_result.get("content", {})
        if content_info.get("significant_change", False):
            changes.append({
                "type": "content",
                "element": "body",
                "change": "content_modified",
                "description": f"Text content changed significantly ({content_info.get('baseline_length', 0)} → {content_info.get('current_length', 0)} chars)",
                "old_value": f"{content_info.get('baseline_length', 0)} characters",
                "new_value": f"{content_info.get('current_length', 0)} characters", 
                "impact": "medium" if abs(content_info.get('length_change', 0)) > 1000 else "low"
            })
            if "content" not in change_types:
                change_types.append("content")
        
        # Determine severity based on impact levels
        high_impact_count = sum(1 for c in changes if c.get("impact") == "high")
        medium_impact_count = sum(1 for c in changes if c.get("impact") == "medium")
        
        if high_impact_count > 0:
            severity = "high"
        elif medium_impact_count > 0:
            severity = "medium"
        elif changes:
            severity = "low"
        else:
            severity = "none"
        
        return {
            "changes_detected": len(changes) > 0,
            "change_types": change_types,
            "changes": changes,
            "summary": {
                "total_changes": len(changes),
                "structural_changes": sum(1 for c in changes if c["type"] == "structure"),
                "content_changes": sum(1 for c in changes if c["type"] == "content"),
                "meta_changes": sum(1 for c in changes if c["type"] == "attributes"),
                "navigation_changes": len(nav_changes),
                "high_impact_changes": high_impact_count,
                "medium_impact_changes": medium_impact_count,
                "severity": severity
            }
        }

    def _create_css_changes_json(self, css_result: Dict[str, Any]) -> Dict[str, Any]:
        """Create structured CSS changes JSON for AI analysis."""
        changes = []
        change_types = []
        
        if css_result.get("has_changes", False):
            # Handle added files
            for file in css_result.get("added", []):
                changes.append({
                    "file": file,
                    "change_type": "added",
                    "description": f"New CSS file added: {file}",
                    "impact": "layout",
                    "severity": "medium"
                })
            
            # Handle removed files
            for file in css_result.get("removed", []):
                changes.append({
                    "file": file,
                    "change_type": "removed",
                    "description": f"CSS file removed: {file}",
                    "impact": "layout",
                    "severity": "high"
                })
            
            # Handle changed files
            for file in css_result.get("changed", []):
                changes.append({
                    "file": file,
                    "change_type": "modified",
                    "description": f"CSS file modified: {file}",
                    "impact": "layout",
                    "severity": "medium"
                })
            
            if changes:
                change_types = ["layout", "styling"]
        
        # Determine severity
        severity = "none"
        if changes:
            if any(c.get("severity") == "high" for c in changes):
                severity = "high"
            elif any(c.get("severity") == "medium" for c in changes):
                severity = "medium"
            else:
                severity = "low"
        
        return {
            "changes_detected": len(changes) > 0,
            "change_types": change_types,
            "files_changed": css_result.get("added", []) + css_result.get("changed", []),
            "changes": changes,
            "summary": {
                "total_changes": len(changes),
                "layout_affecting": len(changes),  # All CSS changes potentially affect layout
                "visual_only": 0,
                "severity": severity
            }
        }

    def _create_js_changes_json(self, js_result: Dict[str, Any]) -> Dict[str, Any]:
        """Create structured JS changes JSON for AI analysis."""
        changes = []
        change_types = []
        
        if js_result.get("has_changes", False):
            # Handle added files
            for file in js_result.get("added", []):
                changes.append({
                    "file": file,
                    "change_type": "added",
                    "description": f"New JavaScript file added: {file}",
                    "functionality_impact": "medium"
                })
            
            # Handle removed files
            for file in js_result.get("removed", []):
                changes.append({
                    "file": file,
                    "change_type": "removed",
                    "description": f"JavaScript file removed: {file}",
                    "functionality_impact": "high"
                })
            
            # Handle changed files
            for file in js_result.get("changed", []):
                changes.append({
                    "file": file,
                    "change_type": "modified",
                    "description": f"JavaScript file modified: {file}",
                    "functionality_impact": "medium"
                })
            
            if changes:
                change_types = ["functionality"]
        
        # Determine severity
        severity = "none"
        if changes:
            if any(c.get("functionality_impact") == "high" for c in changes):
                severity = "high"
            elif any(c.get("functionality_impact") == "medium" for c in changes):
                severity = "medium"
            else:
                severity = "low"
        
        return {
            "changes_detected": len(changes) > 0,
            "change_types": change_types,
            "files_changed": js_result.get("added", []) + js_result.get("changed", []),
            "changes": changes,
            "summary": {
                "total_changes": len(changes),
                "functionality_impact": "high" if severity == "high" else "medium" if severity == "medium" else "none",
                "severity": severity
            }
        }

    def _create_change_summary_json(self, screenshot_result: Dict[str, Any], dom_result: Dict[str, Any], 
                                   css_result: Dict[str, Any], js_result: Dict[str, Any], 
                                   media_result: Dict[str, Any]) -> Dict[str, Any]:
        """Create master change summary JSON for AI analysis."""
        
        # Determine if changes exist in each category
        visual_changes = screenshot_result.get("visual_changes", False)
        html_changes = dom_result.get("has_changes", False) and "error" not in dom_result
        css_changes = css_result.get("has_changes", False)
        js_changes = js_result.get("has_changes", False)
        media_changes = media_result.get("has_changes", False)
        
        changes_detected = any([visual_changes, html_changes, css_changes, js_changes, media_changes])
        
        # Determine overall severity
        severities = []
        if "error" not in screenshot_result and visual_changes:
            ssim_score = screenshot_result.get("ssim_score", 1.0)
            if ssim_score < 0.8:
                severities.append("high")
            elif ssim_score < 0.95:
                severities.append("medium")
            else:
                severities.append("low")
        
        # Add severity from other components
        if css_changes:
            severities.append("medium")  # CSS changes are typically medium impact
        if js_changes:
            severities.append("high")  # JS changes are typically high impact
        if html_changes:
            severities.append("low")  # HTML changes are typically low impact unless structural
        
        if not severities:
            overall_severity = "none"
        elif "high" in severities:
            overall_severity = "high"
        elif "medium" in severities:
            overall_severity = "medium"
        else:
            overall_severity = "low"
        
        # Determine user impact
        if overall_severity == "high":
            user_impact = "high"
        elif visual_changes or css_changes:
            user_impact = "medium"
        elif html_changes:
            user_impact = "low"
        else:
            user_impact = "none"
        
        # Generate affected components (simplified)
        affected_components = []
        if visual_changes:
            affected_components.append("visual_layout")
        if html_changes:
            affected_components.extend(["content", "structure"])
        if css_changes:
            affected_components.append("styling")
        if js_changes:
            affected_components.append("functionality")
        
        # Generate recommendation
        recommendations = []
        if visual_changes:
            recommendations.append("Review visual changes in layout")
        if js_changes:
            recommendations.append("Test JavaScript functionality")
        if css_changes:
            recommendations.append("Verify styling consistency")
        
        recommendation = "; ".join(recommendations) if recommendations else "No changes detected"
        
        return {
            "overall_assessment": {
                "changes_detected": changes_detected,
                "change_severity": overall_severity,
                "user_impact": user_impact,
                "requires_review": overall_severity != "none"
            },
            "change_categories": {
                "visual": {
                    "screenshot_similarity": screenshot_result.get("ssim_score", 1.0),
                    "visual_changes": visual_changes,
                    "layout_shifts": screenshot_result.get("dimensions_changed", False)
                },
                "content": {
                    "title_changed": dom_result.get("title_changed", False),
                    "text_content_changed": dom_result.get("content_changed", False),
                    "structure_changed": dom_result.get("structure_changed", False)
                },
                "technical": {
                    "html_changes": html_changes,
                    "css_changes": css_changes,
                    "js_changes": js_changes,
                    "asset_changes": media_changes
                }
            },
            "affected_components": list(set(affected_components)),
            "recommendation": recommendation,
            "ai_analysis_priority": overall_severity
        }

    def _compare_screenshots(self, baseline_img_path: Path, current_img_path: Path, url: str, diffs_dir: Path) -> Dict[str, Any]:
        """Compare screenshots and generate diff image with SSIM score."""
        if not baseline_img_path.exists() or not current_img_path.exists():
            return {"error": "Screenshots missing", "ssim_score": 0.0}
            
        if not CV2_AVAILABLE:
            return {"error": "OpenCV not available - skipping screenshot comparison", "ssim_score": 0.0}
            
        try:
            # Load images - handle different formats (PNG, JPEG, WebP)
            baseline_img = self._load_image_robust(baseline_img_path)
            current_img = self._load_image_robust(current_img_path)
            
            if baseline_img is None or current_img is None:
                return {"error": "Could not load screenshots", "ssim_score": 0.0}
            
            # Check if dimensions changed
            dimensions_changed = (baseline_img.shape != current_img.shape)
            
            # Resize to same dimensions for comparison
            height = max(baseline_img.shape[0], current_img.shape[0])
            width = max(baseline_img.shape[1], current_img.shape[1])
            baseline_resized = cv2.resize(baseline_img, (width, height))
            current_resized = cv2.resize(current_img, (width, height))
            
            # Convert to grayscale for SSIM
            baseline_gray = cv2.cvtColor(baseline_resized, cv2.COLOR_BGR2GRAY)
            current_gray = cv2.cvtColor(current_resized, cv2.COLOR_BGR2GRAY)
            
            # Calculate SSIM score
            score, diff = ssim(baseline_gray, current_gray, full=True)
            
            # Only create diff image if there are differences
            if score < 1.0:
                # Create diffs directory only when needed
                diffs_dir.mkdir(exist_ok=True)
                
                # Use standardized filename: visual_diff.png
                diff_image_path = diffs_dir / "visual_diff.png"
                
                # Create visual diff image
                diff_normalized = (diff * 255).astype("uint8")
                thresh = cv2.threshold(diff_normalized, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
                contours = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                contours = contours[0] if len(contours) == 2 else contours[1]
                
                # Draw rectangles around differences
                diff_visual = current_resized.copy()
                for c in contours:
                    x, y, w, h = cv2.boundingRect(c)
                    cv2.rectangle(diff_visual, (x, y), (x + w, y + h), (0, 0, 255), 2)
                
                cv2.imwrite(str(diff_image_path), diff_visual)
                
                return {
                    "ssim_score": float(score),
                    "diff_image_path": str(diff_image_path.absolute()),
                    "dimensions_changed": dimensions_changed,
                    "visual_changes": True
                }
            else:
                return {
                    "ssim_score": float(score),
                    "dimensions_changed": dimensions_changed,
                    "visual_changes": False
                }
            
        except Exception as e:
            logger.error(f"Error comparing screenshots for {url}: {e}")
            return {"error": f"Screenshot comparison failed: {str(e)}", "ssim_score": 0.0}

    def _load_image_robust(self, img_path: Path) -> Optional[Any]:
        """Load image robustly handling different formats (PNG, JPEG, WebP)."""
        try:
            # First try OpenCV's imread (handles PNG, JPEG)
            img = cv2.imread(str(img_path))
            if img is not None:
                return img
            
            # If OpenCV fails, try PIL (handles more formats including WebP)
            from PIL import Image
            pil_img = Image.open(img_path)
            
            # Convert to RGB if necessary
            if pil_img.mode != 'RGB':
                pil_img = pil_img.convert('RGB')
            
            # Convert PIL to OpenCV format (BGR)
            import numpy as np
            img_array = np.array(pil_img)
            img_bgr = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
            
            return img_bgr
            
        except Exception as e:
            logger.error(f"Failed to load image {img_path}: {e}")
            return None

    def _compare_single_site(self, baseline_path: Path, current_path: Path, url: str, diffs_dir: Path) -> Dict[str, Any]:
        """Compare a single site's baseline with current state."""
        logger.info(f"Comparing site: {url}")
        
        try:
            # Screenshot comparison
            screenshot_result = self._compare_screenshots(
                baseline_path / "screenshot.png",
                current_path / "screenshot.png",
                url,
                diffs_dir
            )
            
            # Asset comparisons
            css_result = self._compare_assets(baseline_path, current_path, "css")
            js_result = self._compare_assets(baseline_path, current_path, "js") 
            media_result = self._compare_assets(baseline_path, current_path, "media")
            
            # DOM comparison
            dom_result = self._compare_dom(
                baseline_path / "index.html",
                current_path / "index.html"
            )
            
            # Determine if ANY changes exist
            visual_changes = screenshot_result.get("visual_changes", False)
            html_changes = dom_result.get("has_changes", False) and "error" not in dom_result
            css_changes = css_result.get("has_changes", False)
            js_changes = js_result.get("has_changes", False)
            media_changes = media_result.get("has_changes", False)
            
            any_changes = any([visual_changes, html_changes, css_changes, js_changes, media_changes])
            
            # Only create structured diffs if there are changes
            if any_changes:
                # Create diffs directory and structured JSON files
                diffs_dir.mkdir(exist_ok=True)
                logger.info(f"Changes detected for {url}, creating structured diff data")
                
                # Create structured JSON files for AI analysis
                html_changes_json = self._create_html_changes_json(dom_result)
                css_changes_json = self._create_css_changes_json(css_result)
                js_changes_json = self._create_js_changes_json(js_result)
                change_summary_json = self._create_change_summary_json(
                    screenshot_result, dom_result, css_result, js_result, media_result
                )
                
                # Save structured JSON files
                with open(diffs_dir / "html_changes.json", 'w', encoding='utf-8') as f:
                    json.dump(html_changes_json, f, indent=2, ensure_ascii=False)
                
                with open(diffs_dir / "css_changes.json", 'w', encoding='utf-8') as f:
                    json.dump(css_changes_json, f, indent=2, ensure_ascii=False)
                
                with open(diffs_dir / "js_changes.json", 'w', encoding='utf-8') as f:
                    json.dump(js_changes_json, f, indent=2, ensure_ascii=False)
                
                with open(diffs_dir / "change_summary.json", 'w', encoding='utf-8') as f:
                    json.dump(change_summary_json, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Structured diff data saved to {diffs_dir}")
            else:
                logger.info(f"No changes detected for {url}, no diffs directory created")
            
            return {
                "screenshot": screenshot_result,
                "assets": {
                    "css": css_result,
                    "js": js_result,
                    "media": media_result
                },
                "dom": dom_result,
                "changes_detected": any_changes,
                "diffs_created": any_changes
            }
            
        except Exception as e:
            logger.error(f"Error comparing {url}: {e}")
            return {"error": f"Comparison failed: {str(e)}"}

    @classmethod
    def create_url_output_directory(cls, url: str, date_str: Optional[str] = None) -> Path:
        """Create and return the output directory for a specific URL and date."""
        from urllib.parse import urlparse
        
        if date_str is None:
            date_str = settings.get_current_date()
        
        # Convert URL to directory name 
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        path = parsed.path.strip("/").replace("/", "_")
        url_dir_name = f"{domain}_{path}" if path else domain
        
        output_dir = Path("data/comparator") / date_str / url_dir_name
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # NOTE: diffs/ directory will only be created when actual differences are detected
        # This prevents empty diff directories and provides cleaner AI analysis data
        
        return output_dir