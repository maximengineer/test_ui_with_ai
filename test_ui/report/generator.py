"""HTML report generator."""
import asyncio
import base64
import json
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
import shutil
from jinja2 import Template
import httpx
from loguru import logger
from ..config import settings

class ReportGenerator:
    def __init__(self, config, gemini_url: str):
        self.config = config
        self.gemini_url = gemini_url
        self.client = httpx.AsyncClient(timeout=60.0)
    
    def discover_comparison_data(self, comparator_root: Path, date: str) -> Dict[str, List[Dict]]:
        """Scan data/comparator/{date}/ for URLs and categorize by analyzing comparison_results.json"""
        date_dir = comparator_root / date
        if not date_dir.exists():
            return {"with_changes": [], "without_changes": []}
        
        urls_with_changes = []
        urls_without_changes = []
        
        for url_dir in date_dir.iterdir():
            if url_dir.is_dir():
                comparison_file = url_dir / "comparison_results.json"
                if not comparison_file.exists():
                    logger.warning(f"No comparison_results.json found for {url_dir.name}")
                    continue
                
                try:
                    # Load and analyze comparison results
                    with open(comparison_file, 'r', encoding='utf-8') as f:
                        comparison_data = json.load(f)
                    
                    # Check if changes were detected based on the result data
                    result = comparison_data.get("result", {})
                    changes_detected = result.get("changes_detected", False)
                    
                    # Double-check with multiple indicators for robustness
                    visual_changes = result.get("screenshot", {}).get("visual_changes", False)
                    dom_changes = result.get("dom", {}).get("has_changes", False)
                    css_changes = result.get("assets", {}).get("css", {}).get("has_changes", False)
                    js_changes = result.get("assets", {}).get("js", {}).get("has_changes", False)
                    media_changes = result.get("assets", {}).get("media", {}).get("has_changes", False)
                    
                    # Any of these indicators means there are changes
                    has_any_changes = any([changes_detected, visual_changes, dom_changes, 
                                         css_changes, js_changes, media_changes])
                    
                    if has_any_changes:
                        # Has changes - look for diffs directory
                        diffs_dir = url_dir / "diffs"
                        urls_with_changes.append({
                            "url_name": url_dir.name,
                            "url_dir": url_dir,
                            "structured_data_path": diffs_dir if diffs_dir.exists() else None,
                            "has_changes": True,
                            "comparison_data": comparison_data
                        })
                    else:
                        # No changes detected
                        urls_without_changes.append({
                            "url_name": url_dir.name,
                            "url_dir": url_dir,
                            "has_changes": False,
                            "comparison_data": comparison_data
                        })
                        
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"Error reading comparison data for {url_dir.name}: {e}")
                    continue
        
        logger.info(f"Discovered {len(urls_with_changes)} URLs with changes, {len(urls_without_changes)} without changes")
        return {
            "with_changes": urls_with_changes,
            "without_changes": urls_without_changes
        }
    
    def create_no_changes_analysis(self, url_name: str) -> Dict:
        """Create standardized analysis JSON for URLs with no changes"""
        return {
            "overall_severity": "SAFE",
            "business_impact": "NONE",
            "detailed_analysis": {
                "visual_changes": [],
                "functional_impact": [],
                "technical_changes": ["No changes detected in HTML, CSS, JavaScript, or visual elements"]
            },
            "recommendations": {
                "immediate_actions": [],
                "review_items": [],
                "acceptance_criteria": "No action required - page remains unchanged"
            },
            "confidence_score": 1.0,
            "analysis_type": "no_changes_detected",
            "timestamp": settings.get_current_datetime(),
            "url": url_name
        }
    
    def process_urls_without_changes(self, urls_without_changes: List[Dict], report_date: str) -> List[Dict]:
        """Process URLs without changes by creating no-changes JSON files"""
        processed_results = []
        
        for url_data in urls_without_changes:
            # Create report directory for this URL
            report_url_dir = Path("data/report") / report_date / url_data["url_name"]
            report_url_dir.mkdir(parents=True, exist_ok=True)
            
            # Create no-changes analysis
            no_changes_analysis = self.create_no_changes_analysis(url_data["url_name"])
            
            # Store analysis JSON
            ai_analysis_file = report_url_dir / "ai_analysis.json"
            ai_analysis_file.write_text(json.dumps(no_changes_analysis, indent=2), encoding='utf-8')
            
            logger.info(f"Created no-changes analysis for {url_data['url_name']}")
            
            processed_results.append({
                "url": url_data["url_name"],
                "has_changes": False,
                "ai_analysis": no_changes_analysis,
                "report_path": report_url_dir
            })
        
        return processed_results
    
    def load_structured_data(self, diffs_dir: Path) -> Dict:
        """Load all structured diff JSON files for a URL"""
        if not diffs_dir or not diffs_dir.exists():
            logger.warning(f"Diffs directory not found: {diffs_dir}")
            return {}
        
        structured_data = {}
        
        # Define expected JSON files and their keys
        json_files = {
            "change_summary": "change_summary.json",
            "html_changes": "html_changes.json", 
            "css_changes": "css_changes.json",
            "js_changes": "js_changes.json"
        }
        
        # Load each JSON file
        for key, filename in json_files.items():
            file_path = diffs_dir / filename
            if file_path.exists():
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        structured_data[key] = json.load(f)
                    logger.debug(f"Loaded {filename} for structured data")
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Error loading {filename}: {e}")
                    structured_data[key] = {"error": f"Failed to load {filename}: {str(e)}"}
            else:
                logger.warning(f"Missing structured file: {filename}")
                structured_data[key] = {"error": f"File not found: {filename}"}
        
        # Check for visual diff image
        visual_diff_path = diffs_dir / "visual_diff.png"
        structured_data["visual_diff_image"] = visual_diff_path if visual_diff_path.exists() else None
        
        # Add metadata
        files_loaded_count = 0
        for k, v in structured_data.items():
            if k not in ["metadata", "visual_diff_image"]:
                if isinstance(v, dict) and "error" not in v:
                    files_loaded_count += 1
                elif not isinstance(v, dict):
                    files_loaded_count += 1
        
        structured_data["metadata"] = {
            "diffs_directory": str(diffs_dir.absolute()),
            "files_loaded": files_loaded_count,
            "timestamp": settings.get_current_datetime()
        }
        
        logger.info(f"Loaded structured data from {diffs_dir} - {structured_data['metadata']['files_loaded']} files")
        return structured_data
    
    def load_screenshots(self, url_dir: Path, comparison_data: Dict = None) -> Dict:
        """Load screenshot files for a URL (baseline, current, visual diff)"""
        screenshots = {}
        screenshot_files = {}
        
        # Try to get paths from comparison_data if available
        if comparison_data:
            result = comparison_data.get("result", {})
            metadata = comparison_data.get("metadata", {})
            
            # Extract baseline and current paths from metadata
            baseline_path = Path(metadata.get("baseline_path", ""))
            current_path = Path(metadata.get("current_path", ""))
            
            # Construct screenshot paths
            if baseline_path.exists():
                baseline_screenshot = baseline_path / url_dir.name / "screenshot.png"
                if baseline_screenshot.exists():
                    screenshot_files["baseline"] = baseline_screenshot
            
            if current_path.exists():
                current_screenshot = current_path / url_dir.name / "screenshot.png"  
                if current_screenshot.exists():
                    screenshot_files["current"] = current_screenshot
            
            # Get visual diff path from screenshot result
            screenshot_result = result.get("screenshot", {})
            diff_image_path = screenshot_result.get("diff_image_path")
            if diff_image_path and Path(diff_image_path).exists():
                screenshot_files["visual_diff"] = Path(diff_image_path)
        
        # Fallback: Check for visual diff in diffs directory
        if "visual_diff" not in screenshot_files:
            diffs_dir = url_dir / "diffs"
            if diffs_dir.exists():
                visual_diff_path = diffs_dir / "visual_diff.png"
                if visual_diff_path.exists():
                    screenshot_files["visual_diff"] = visual_diff_path
        
        # Load screenshots as base64
        for key, file_path in screenshot_files.items():
            if file_path.exists():
                try:
                    screenshots[f"{key}_b64"] = base64.b64encode(file_path.read_bytes()).decode('utf-8')
                    screenshots[f"{key}_path"] = str(file_path.absolute())
                    logger.debug(f"Loaded {key} screenshot: {file_path}")
                except IOError as e:
                    logger.error(f"Error loading {key} screenshot {file_path}: {e}")
                    screenshots[f"{key}_error"] = str(e)
            else:
                logger.warning(f"Screenshot not found: {file_path}")
                screenshots[f"{key}_missing"] = str(file_path)
        
        logger.info(f"Loaded {len([k for k in screenshots.keys() if k.endswith('_b64')])} screenshots for {url_dir.name}")
        return screenshots
    
    def create_system_context(self) -> str:
        """Comprehensive system prompt explaining our data structure and expectations"""
        return """
You are an expert front-end developer and QA engineer analyzing structured web UI regression data.

DATA FORMAT EXPLANATION:
- change_summary: Overall assessment with severity levels (high/medium/low/none) and user impact analysis
- html_changes: Specific DOM elements added/removed/modified with actual code snippets showing what changed
- css_changes: Stylesheet modifications with selectors, properties, and impact assessment (layout/visual/functional)
- js_changes: JavaScript function/variable changes with code examples and functionality impact
- screenshots: Visual comparison with baseline, current, and diff images highlighting changes

ANALYSIS EXPECTATIONS:
1. Correlate visual changes with technical changes - explain WHY the visual changes occurred based on the code changes
2. Assess business impact focusing on user experience, functionality, and accessibility
3. Prioritize issues by severity and user impact - distinguish between breaking changes vs. cosmetic updates
4. Provide specific, actionable recommendations with clear next steps
5. Consider context - some changes may be intentional updates vs. regressions

RESPONSE FORMAT REQUIRED:
{
    "overall_severity": "CRITICAL|WARNING|SAFE",
    "business_impact": "HIGH|MEDIUM|LOW", 
    "detailed_analysis": {
        "visual_changes": ["List specific visual changes you observe in the screenshots"],
        "functional_impact": ["Describe potential functionality impacts based on code changes"],
        "technical_correlation": ["Explain how the code changes caused the visual changes"]
    },
    "recommendations": {
        "immediate_actions": ["Urgent actions that must be taken now"],
        "review_items": ["Items that require developer/stakeholder review"],
        "acceptance_criteria": "Clear criteria for accepting or rejecting these changes"
    },
    "confidence_score": 0.95,
    "reasoning": "Brief explanation of your assessment methodology and confidence level"
}

SEVERITY GUIDELINES:
- CRITICAL: Breaking changes that impact core functionality, accessibility, or cause major layout issues
- WARNING: Noticeable changes that may or may not be intentional, require human review
- SAFE: Minor cosmetic changes with no functional impact

Focus on being helpful to development teams by providing actionable insights, not just describing what you see.
"""
    
    def create_ai_request(self, url: str, structured_data: Dict, screenshots: Dict) -> Dict:
        """Create comprehensive AI analysis request with full context"""
        
        # Extract key metrics for context
        change_summary = structured_data.get("change_summary", {})
        html_changes = structured_data.get("html_changes", {})
        css_changes = structured_data.get("css_changes", {})
        js_changes = structured_data.get("js_changes", {})
        
        # Create comprehensive request payload
        return {
            "url": url,
            "system_context": self.create_system_context(),
            "analysis_request": {
                "analysis_type": "comprehensive_ui_regression",
                "priority": "high" if change_summary.get("overall_assessment", {}).get("change_severity") == "high" else "medium",
                "requires_detailed_analysis": True
            },
            "structured_data": {
                "change_summary": change_summary,
                "html_changes": html_changes, 
                "css_changes": css_changes,
                "js_changes": js_changes,
                "metadata": structured_data.get("metadata", {})
            },
            "screenshots": {
                "baseline": screenshots.get("baseline_b64"),
                "current": screenshots.get("current_b64"),
                "visual_diff": screenshots.get("visual_diff_b64")
            },
            "context_hints": {
                "total_html_changes": html_changes.get("summary", {}).get("total_changes", 0),
                "css_changes_detected": css_changes.get("changes_detected", False),
                "js_changes_detected": js_changes.get("changes_detected", False),
                "has_visual_differences": screenshots.get("visual_diff_b64") is not None,
                "change_severity": change_summary.get("overall_assessment", {}).get("change_severity", "unknown")
            }
        }
    
    async def send_to_ai_analyzer(self, ai_request: Dict, max_retries: int = 3) -> Dict:
        """Send comprehensive analysis request to AI analyzer service with retry logic and enhanced error handling"""
        
        url = ai_request.get('url', 'unknown')
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Exponential backoff: 2, 4, 8 seconds
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying AI analysis for {url} (attempt {attempt + 1}/{max_retries}) after {wait_time}s delay")
                    await asyncio.sleep(wait_time)
                else:
                    logger.info(f"Sending structured analysis request for {url} to AI analyzer")
                
                # Validate request before sending
                validation_errors = self._validate_ai_request(ai_request)
                if validation_errors:
                    raise ValueError(f"Invalid AI request: {'; '.join(validation_errors)}")
                
                response = await self.client.post(
                    f"{self.gemini_url}/api/compare",
                    json=ai_request,
                    timeout=120.0  # Extended timeout for comprehensive analysis
                )
                response.raise_for_status()
                
                # Validate response
                ai_analysis = response.json()
                validation_errors = self._validate_ai_response(ai_analysis)
                if validation_errors:
                    raise ValueError(f"Invalid AI response: {'; '.join(validation_errors)}")
                
                logger.info(f"Received AI analysis for {url} - Severity: {ai_analysis.get('overall_severity', 'UNKNOWN')} (attempt {attempt + 1})")
                return ai_analysis
                
            except httpx.TimeoutException as e:
                last_error = e
                logger.error(f"AI analyzer timeout for {url} (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    break  # Don't retry on final attempt
                    
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.error(f"AI analyzer HTTP error for {url} (attempt {attempt + 1}): {e.response.status_code}")
                
                # Don't retry on client errors (4xx)
                if 400 <= e.response.status_code < 500:
                    break
                elif attempt == max_retries - 1:
                    break  # Don't retry on final attempt for server errors
                    
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
                logger.error(f"AI response validation error for {url} (attempt {attempt + 1}): {e}")
                # Don't retry validation errors - they likely won't resolve
                break
                
            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error during AI analysis for {url} (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    break
        
        # All retries exhausted - return structured error response
        logger.error(f"All {max_retries} attempts failed for AI analysis of {url}. Last error: {last_error}")
        return self._create_error_response(url, last_error, max_retries)
    
    def _validate_ai_request(self, ai_request: Dict) -> List[str]:
        """Validate AI request structure before sending"""
        errors = []
        
        required_fields = ['url', 'system_context', 'structured_data', 'screenshots', 'context_hints']
        for field in required_fields:
            if field not in ai_request:
                errors.append(f"Missing required field: {field}")
            elif ai_request[field] is None:
                errors.append(f"Field is None: {field}")
        
        if 'structured_data' in ai_request:
            structured_data = ai_request['structured_data']
            if not isinstance(structured_data, dict):
                errors.append("structured_data must be a dictionary")
            else:
                required_sub_fields = ['html_changes', 'css_changes', 'js_changes']
                for sub_field in required_sub_fields:
                    if sub_field not in structured_data:
                        errors.append(f"Missing structured_data.{sub_field}")
        
        return errors
    
    def _validate_ai_response(self, ai_response: Dict) -> List[str]:
        """Validate AI response structure"""
        errors = []
        
        required_fields = ['overall_severity', 'business_impact', 'detailed_analysis', 'recommendations', 'confidence_score']
        for field in required_fields:
            if field not in ai_response:
                errors.append(f"Missing required response field: {field}")
        
        # Validate severity values
        if 'overall_severity' in ai_response:
            valid_severities = ['CRITICAL', 'WARNING', 'SAFE', 'ERROR']
            if ai_response['overall_severity'] not in valid_severities:
                errors.append(f"Invalid overall_severity: {ai_response['overall_severity']}")
        
        # Validate confidence score
        if 'confidence_score' in ai_response:
            try:
                confidence = float(ai_response['confidence_score'])
                if not (0.0 <= confidence <= 1.0):
                    errors.append(f"confidence_score must be between 0.0 and 1.0: {confidence}")
            except (TypeError, ValueError):
                errors.append("confidence_score must be a number")
        
        return errors
    
    def _create_error_response(self, url: str, last_error: Exception, max_retries: int) -> Dict:
        """Create structured error response after all retries exhausted"""
        
        error_type = type(last_error).__name__
        error_message = str(last_error)
        
        if isinstance(last_error, httpx.TimeoutException):
            return {
                "overall_severity": "ERROR",
                "business_impact": "HIGH",
                "detailed_analysis": {
                    "visual_changes": [],
                    "functional_impact": [f"Analysis timeout after {max_retries} attempts - service may be overloaded"],
                    "technical_correlation": []
                },
                "recommendations": {
                    "immediate_actions": ["Check AI analyzer service health", "Consider reducing analysis complexity"],
                    "review_items": ["Service performance optimization", "Network connectivity"],
                    "acceptance_criteria": "Resolve timeout before proceeding with analysis"
                },
                "confidence_score": 0.0,
                "reasoning": f"Service timeout after {max_retries} retries",
                "error_metadata": {
                    "error_type": error_type,
                    "max_retries": max_retries,
                    "final_error": error_message[:200]
                }
            }
        elif isinstance(last_error, httpx.HTTPStatusError):
            return {
                "overall_severity": "ERROR",
                "business_impact": "HIGH",
                "detailed_analysis": {
                    "visual_changes": [],
                    "functional_impact": [f"AI analyzer service error: HTTP {last_error.response.status_code}"],
                    "technical_correlation": []
                },
                "recommendations": {
                    "immediate_actions": ["Check AI analyzer service logs", "Verify service configuration"],
                    "review_items": ["Service deployment status"],
                    "acceptance_criteria": "Resolve service error before analysis"
                },
                "confidence_score": 0.0,
                "reasoning": f"HTTP error {last_error.response.status_code} after {max_retries} retries",
                "error_metadata": {
                    "error_type": error_type,
                    "status_code": last_error.response.status_code,
                    "max_retries": max_retries
                }
            }
        else:
            return {
                "overall_severity": "ERROR",
                "business_impact": "HIGH",
                "detailed_analysis": {
                    "visual_changes": [],
                    "functional_impact": [f"Analysis failed due to system error: {error_type}"],
                    "technical_correlation": []
                },
                "recommendations": {
                    "immediate_actions": ["Check system logs", "Verify data integrity", "Contact support if issue persists"],
                    "review_items": ["System stability", "Data format validation"],
                    "acceptance_criteria": "Resolve system error before analysis"
                },
                "confidence_score": 0.0,
                "reasoning": f"System error after {max_retries} retries: {error_message[:100]}",
                "error_metadata": {
                    "error_type": error_type,
                    "max_retries": max_retries,
                    "error_details": error_message[:200]
                }
            }
    
    async def process_single_url(self, url_data: Dict, report_date: str) -> Dict:
        """Process one URL with full structured data analysis and persistent storage"""
        
        url_name = url_data['url_name']
        logger.info(f"Processing URL: {url_name}")
        
        # Create report directory structure: data/report/{date}/{url}/
        report_url_dir = Path("data/report") / report_date / url_name
        report_url_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Load structured diff data
            structured_data = self.load_structured_data(url_data['structured_data_path'])
            if not structured_data:
                raise ValueError("No structured data loaded")
            
            # Load screenshots
            screenshots = self.load_screenshots(url_data['url_dir'], url_data.get('comparison_data'))
            if not any(k.endswith('_b64') for k in screenshots.keys()):
                raise ValueError("No screenshots loaded")
            
            # Create comprehensive AI request
            ai_request = self.create_ai_request(url_name, structured_data, screenshots)
            
            # Send to AI analyzer
            ai_response = await self.send_to_ai_analyzer(ai_request)
            
            # Store structured data for reference
            structured_data_file = report_url_dir / "structured_data.json"
            structured_data_file.write_text(
                json.dumps(structured_data, indent=2, default=str), 
                encoding='utf-8'
            )
            
            # Store AI response persistently
            ai_analysis_file = report_url_dir / "ai_analysis.json"
            ai_analysis_file.write_text(
                json.dumps(ai_response, indent=2), 
                encoding='utf-8'
            )
            
            # Copy screenshots to report directory for HTML report access
            screenshots_dir = report_url_dir / "screenshots"
            screenshots_dir.mkdir(exist_ok=True)
            
            # Copy visual assets if they exist
            for screenshot_type in ['baseline', 'current', 'visual_diff']:
                path_key = f"{screenshot_type}_path"
                if path_key in screenshots and screenshots[path_key]:
                    source_path = Path(screenshots[path_key])
                    if source_path.exists():
                        dest_path = screenshots_dir / f"{screenshot_type}.png"
                        import shutil
                        shutil.copy2(source_path, dest_path)
                        logger.debug(f"Copied {screenshot_type} screenshot to {dest_path}")
            
            logger.info(f"Successfully processed {url_name} - Severity: {ai_response.get('overall_severity', 'UNKNOWN')}")
            
            return {
                "url": url_name,
                "structured_data": structured_data,
                "ai_analysis": ai_response,
                "report_path": report_url_dir,
                "processing_status": "success",
                "screenshots_available": [k.replace('_b64', '') for k in screenshots.keys() if k.endswith('_b64')]
            }
            
        except Exception as e:
            logger.error(f"Error processing URL {url_name}: {e}")
            
            # Create error analysis file
            error_analysis = {
                "overall_severity": "ERROR",
                "business_impact": "HIGH",
                "detailed_analysis": {
                    "visual_changes": [],
                    "functional_impact": [f"Processing error: {str(e)}"],
                    "technical_correlation": []
                },
                "recommendations": {
                    "immediate_actions": ["Check processing logs", "Verify data integrity"],
                    "review_items": [f"URL: {url_name}"],
                    "acceptance_criteria": "Resolve processing error and retry"
                },
                "confidence_score": 0.0,
                "reasoning": f"Processing failed: {str(e)}",
                "error_details": {
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "url_name": url_name,
                    "timestamp": settings.get_current_datetime()
                }
            }
            
            # Store error analysis
            ai_analysis_file = report_url_dir / "ai_analysis.json"
            ai_analysis_file.write_text(
                json.dumps(error_analysis, indent=2),
                encoding='utf-8'
            )
            
            return {
                "url": url_name,
                "structured_data": {},
                "ai_analysis": error_analysis,
                "report_path": report_url_dir,
                "processing_status": "error",
                "error": str(e)
            }
    
    def aggregate_analyses(self, all_url_results: List[Dict]) -> Dict:
        """Create cross-URL insights and patterns from all processed URLs"""
        
        logger.info(f"Aggregating analysis from {len(all_url_results)} URLs")
        
        # Categorize results by severity and status
        critical_issues = []
        warnings = []
        safe_changes = []
        errors = []
        no_changes = []
        
        for result in all_url_results:
            ai_analysis = result.get("ai_analysis", {})
            severity = ai_analysis.get("overall_severity", "UNKNOWN")
            
            if severity == "CRITICAL":
                critical_issues.append(result)
            elif severity == "WARNING":
                warnings.append(result)
            elif severity == "SAFE":
                safe_changes.append(result)
            elif severity == "ERROR":
                errors.append(result)
            elif ai_analysis.get("analysis_type") == "no_changes_detected":
                no_changes.append(result)
            else:
                # Unknown severity - treat as warning
                warnings.append(result)
        
        # Calculate summary statistics
        total_urls = len(all_url_results)
        urls_with_changes = len([r for r in all_url_results if r.get("processing_status") == "success" and 
                                r.get("ai_analysis", {}).get("analysis_type") != "no_changes_detected"])
        
        # Generate aggregate insights
        aggregation_result = {
            "summary": {
                "total_urls_analyzed": total_urls,
                "urls_with_changes": urls_with_changes,
                "urls_without_changes": len(no_changes),
                "critical_issues": len(critical_issues),
                "warnings": len(warnings),
                "safe_changes": len(safe_changes),
                "errors": len(errors),
                "analysis_timestamp": settings.get_current_datetime()
            },
            "severity_breakdown": {
                "critical": [{"url": r["url"], "impact": r["ai_analysis"].get("business_impact", "UNKNOWN")} 
                           for r in critical_issues],
                "warnings": [{"url": r["url"], "impact": r["ai_analysis"].get("business_impact", "UNKNOWN")} 
                           for r in warnings],
                "safe": [{"url": r["url"]} for r in safe_changes],
                "errors": [{"url": r["url"], "error": r.get("error", "Processing error")} for r in errors]
            },
            "patterns": self.identify_common_patterns(all_url_results),
            "recommendations": self.generate_global_recommendations(all_url_results),
            "confidence_metrics": self.calculate_confidence_metrics(all_url_results)
        }
        
        logger.info(f"Aggregation complete - {len(critical_issues)} critical, {len(warnings)} warnings, {len(safe_changes)} safe")
        return aggregation_result
    
    def identify_common_patterns(self, all_url_results: List[Dict]) -> Dict:
        """Identify common patterns and themes across multiple URLs"""
        
        patterns = {
            "common_html_changes": {},
            "recurring_issues": [],
            "affected_components": {},
            "change_types": {},
            "business_impact_distribution": {}
        }
        
        # Analyze HTML changes across URLs
        html_change_types = {}
        total_html_changes = 0
        
        for result in all_url_results:
            structured_data = result.get("structured_data", {})
            html_changes = structured_data.get("html_changes", {})
            
            # Count change types
            if "changes" in html_changes:
                for change in html_changes["changes"][:10]:  # Limit to avoid huge data
                    change_type = change.get("type", "unknown")
                    html_change_types[change_type] = html_change_types.get(change_type, 0) + 1
                    total_html_changes += 1
        
        patterns["common_html_changes"] = {
            "total_changes": total_html_changes,
            "change_types": dict(sorted(html_change_types.items(), key=lambda x: x[1], reverse=True)[:5])
        }
        
        # Analyze business impact patterns
        impact_distribution = {}
        for result in all_url_results:
            ai_analysis = result.get("ai_analysis", {})
            impact = ai_analysis.get("business_impact", "UNKNOWN")
            impact_distribution[impact] = impact_distribution.get(impact, 0) + 1
        
        patterns["business_impact_distribution"] = impact_distribution
        
        # Identify recurring issues
        functional_impacts = []
        for result in all_url_results:
            ai_analysis = result.get("ai_analysis", {})
            detailed_analysis = ai_analysis.get("detailed_analysis", {})
            impacts = detailed_analysis.get("functional_impact", [])
            functional_impacts.extend(impacts[:3])  # Take first 3 per URL
        
        # Find most common functional impacts (simple pattern matching)
        impact_counts = {}
        for impact in functional_impacts:
            # Simple keyword extraction for pattern detection
            keywords = impact.lower().split()
            for keyword in keywords:
                if len(keyword) > 3:  # Skip short words
                    impact_counts[keyword] = impact_counts.get(keyword, 0) + 1
        
        # Get top recurring keywords
        recurring_keywords = sorted(impact_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        patterns["recurring_issues"] = [
            {"keyword": keyword, "frequency": count, "urls_affected": min(count, len(all_url_results))}
            for keyword, count in recurring_keywords if count > 1
        ]
        
        return patterns
    
    def generate_global_recommendations(self, all_url_results: List[Dict]) -> Dict:
        """Generate global recommendations based on cross-URL analysis"""
        
        # Count critical and warning issues
        critical_count = len([r for r in all_url_results 
                            if r.get("ai_analysis", {}).get("overall_severity") == "CRITICAL"])
        warning_count = len([r for r in all_url_results 
                           if r.get("ai_analysis", {}).get("overall_severity") == "WARNING"])
        error_count = len([r for r in all_url_results 
                          if r.get("ai_analysis", {}).get("overall_severity") == "ERROR"])
        
        recommendations = {
            "immediate_actions": [],
            "strategic_actions": [],
            "process_improvements": [],
            "monitoring_suggestions": []
        }
        
        # Generate recommendations based on severity distribution
        if critical_count > 0:
            recommendations["immediate_actions"].append(
                f"Address {critical_count} critical issue{'s' if critical_count != 1 else ''} before deployment"
            )
            recommendations["immediate_actions"].append("Conduct thorough manual testing of affected functionality")
        
        if warning_count > 0:
            recommendations["strategic_actions"].append(
                f"Review {warning_count} warning{'s' if warning_count != 1 else ''} for intentional vs. unintentional changes"
            )
        
        if error_count > 0:
            recommendations["process_improvements"].append(
                f"Investigate {error_count} analysis error{'s' if error_count != 1 else ''} to improve system reliability"
            )
        
        # Add general recommendations based on patterns
        total_changes = len([r for r in all_url_results if r.get("processing_status") == "success"])
        if total_changes > len(all_url_results) * 0.7:  # More than 70% of URLs changed
            recommendations["strategic_actions"].append(
                "High change volume detected - consider impact on user experience consistency"
            )
        
        recommendations["monitoring_suggestions"].extend([
            "Set up automated regression testing for frequently changing components",
            "Monitor user feedback for any unexpected behavior",
            "Consider implementing gradual rollout for significant changes"
        ])
        
        return recommendations
    
    def calculate_confidence_metrics(self, all_url_results: List[Dict]) -> Dict:
        """Calculate enhanced confidence metrics with data quality assessment"""
        
        confidence_scores = []
        successful_analyses = 0
        data_quality_scores = []
        analysis_completeness_scores = []
        
        for result in all_url_results:
            ai_analysis = result.get("ai_analysis", {})
            structured_data = result.get("structured_data", {})
            
            if result.get("processing_status") == "success":
                successful_analyses += 1
                
                # AI confidence score
                ai_confidence = ai_analysis.get("confidence_score", 0.0)
                if isinstance(ai_confidence, (int, float)) and 0 <= ai_confidence <= 1:
                    confidence_scores.append(ai_confidence)
                
                # Data quality score (based on data completeness)
                data_quality = self._calculate_data_quality_score(structured_data, result)
                data_quality_scores.append(data_quality)
                
                # Analysis completeness score
                analysis_completeness = self._calculate_analysis_completeness_score(ai_analysis)
                analysis_completeness_scores.append(analysis_completeness)
        
        # Calculate aggregate scores
        if confidence_scores:
            avg_ai_confidence = sum(confidence_scores) / len(confidence_scores)
            min_ai_confidence = min(confidence_scores)
            max_ai_confidence = max(confidence_scores)
        else:
            avg_ai_confidence = min_ai_confidence = max_ai_confidence = 0.0
            
        if data_quality_scores:
            avg_data_quality = sum(data_quality_scores) / len(data_quality_scores)
            min_data_quality = min(data_quality_scores)
        else:
            avg_data_quality = min_data_quality = 0.0
            
        if analysis_completeness_scores:
            avg_analysis_completeness = sum(analysis_completeness_scores) / len(analysis_completeness_scores)
        else:
            avg_analysis_completeness = 0.0
        
        # Calculate composite confidence score
        composite_confidence = self._calculate_composite_confidence(
            avg_ai_confidence, avg_data_quality, avg_analysis_completeness, len(all_url_results)
        )
        
        # Determine confidence level
        confidence_level = self._determine_confidence_level(composite_confidence, min_data_quality, successful_analyses / len(all_url_results) if all_url_results else 0.0)
        
        return {
            "composite_confidence": round(composite_confidence, 3),
            "confidence_level": confidence_level,
            "ai_confidence": {
                "average": round(avg_ai_confidence, 3),
                "min": round(min_ai_confidence, 3),
                "max": round(max_ai_confidence, 3)
            },
            "data_quality": {
                "average": round(avg_data_quality, 3),
                "min": round(min_data_quality, 3)
            },
            "analysis_completeness": {
                "average": round(avg_analysis_completeness, 3)
            },
            "successful_analyses": successful_analyses,
            "total_urls": len(all_url_results),
            "success_rate": round(successful_analyses / len(all_url_results), 3) if all_url_results else 0.0,
            "quality_indicators": self._generate_quality_indicators(all_url_results),
            "validation_warnings": self._generate_validation_warnings(all_url_results)
        }
    
    def _calculate_data_quality_score(self, structured_data: Dict, result: Dict) -> float:
        """Calculate data quality score based on completeness and validity"""
        score = 0.0
        max_score = 1.0
        
        # Check structured data completeness (40% of score)
        expected_keys = ['html_changes', 'css_changes', 'js_changes', 'metadata']
        present_keys = sum(1 for key in expected_keys if key in structured_data and structured_data[key])
        score += (present_keys / len(expected_keys)) * 0.4
        
        # Check screenshot availability (30% of score)
        screenshots_available = result.get("screenshots_available", [])
        expected_screenshots = ['baseline', 'current', 'visual_diff']
        screenshot_score = sum(1 for shot in expected_screenshots if shot in screenshots_available)
        score += (screenshot_score / len(expected_screenshots)) * 0.3
        
        # Check data richness - HTML changes with details (20% of score)
        html_changes = structured_data.get("html_changes", {})
        if html_changes and isinstance(html_changes, dict):
            changes = html_changes.get("changes", [])
            if changes and len(changes) > 0:
                # Check for detailed changes with code snippets
                detailed_changes = sum(1 for change in changes[:10] if change.get("code_snippet"))
                score += (min(detailed_changes, 5) / 5) * 0.2
        
        # Processing status (10% of score)
        if result.get("processing_status") == "success":
            score += 0.1
            
        return min(score, max_score)
    
    def _calculate_analysis_completeness_score(self, ai_analysis: Dict) -> float:
        """Calculate how complete the AI analysis is"""
        score = 0.0
        
        # Required fields presence (60% of score)
        required_fields = ['overall_severity', 'business_impact', 'detailed_analysis', 'recommendations', 'confidence_score']
        present_fields = sum(1 for field in required_fields if field in ai_analysis and ai_analysis[field] is not None)
        score += (present_fields / len(required_fields)) * 0.6
        
        # Detailed analysis completeness (25% of score)
        detailed_analysis = ai_analysis.get("detailed_analysis", {})
        if isinstance(detailed_analysis, dict):
            detail_fields = ['visual_changes', 'functional_impact', 'technical_correlation']
            present_details = sum(1 for field in detail_fields 
                                if field in detailed_analysis and 
                                isinstance(detailed_analysis[field], list) and 
                                len(detailed_analysis[field]) > 0)
            score += (present_details / len(detail_fields)) * 0.25
        
        # Recommendations completeness (15% of score)
        recommendations = ai_analysis.get("recommendations", {})
        if isinstance(recommendations, dict):
            rec_fields = ['immediate_actions', 'review_items', 'acceptance_criteria']
            present_recs = sum(1 for field in rec_fields if field in recommendations and recommendations[field])
            score += (present_recs / len(rec_fields)) * 0.15
            
        return min(score, 1.0)
    
    def _calculate_composite_confidence(self, ai_confidence: float, data_quality: float, analysis_completeness: float, sample_size: int) -> float:
        """Calculate composite confidence score combining multiple factors"""
        
        # Weight the different components
        ai_weight = 0.5      # AI confidence is most important
        quality_weight = 0.3  # Data quality affects reliability
        completeness_weight = 0.2  # Analysis completeness ensures thoroughness
        
        base_score = (ai_confidence * ai_weight + 
                     data_quality * quality_weight + 
                     analysis_completeness * completeness_weight)
        
        # Apply sample size adjustment (reduce confidence for very small samples)
        if sample_size < 3:
            size_penalty = 0.1 * (3 - sample_size)  # Reduce by 10% per missing URL below 3
            base_score = max(0.0, base_score - size_penalty)
        
        return min(base_score, 1.0)
    
    def _determine_confidence_level(self, composite_confidence: float, min_data_quality: float, success_rate: float) -> str:
        """Determine qualitative confidence level"""
        
        # High confidence: good composite score, high data quality, high success rate
        if composite_confidence >= 0.8 and min_data_quality >= 0.7 and success_rate >= 0.9:
            return "HIGH"
        # Medium confidence: acceptable scores
        elif composite_confidence >= 0.6 and min_data_quality >= 0.5 and success_rate >= 0.7:
            return "MEDIUM"
        # Low confidence: poor scores or low success rate
        elif composite_confidence >= 0.4 and success_rate >= 0.5:
            return "LOW"
        else:
            return "VERY_LOW"
    
    def _generate_quality_indicators(self, all_url_results: List[Dict]) -> Dict:
        """Generate quality indicators for the analysis"""
        
        indicators = {
            "data_completeness": 0.0,
            "screenshot_coverage": 0.0,
            "detailed_analysis_coverage": 0.0,
            "error_rate": 0.0
        }
        
        if not all_url_results:
            return indicators
        
        total_urls = len(all_url_results)
        
        # Data completeness
        complete_data_count = 0
        screenshot_coverage_count = 0
        detailed_analysis_count = 0
        error_count = 0
        
        for result in all_url_results:
            # Data completeness check
            structured_data = result.get("structured_data", {})
            if all(key in structured_data for key in ['html_changes', 'css_changes', 'js_changes']):
                complete_data_count += 1
                
            # Screenshot coverage
            screenshots_available = result.get("screenshots_available", [])
            if len(screenshots_available) >= 2:  # At least baseline and current
                screenshot_coverage_count += 1
                
            # Detailed analysis coverage
            ai_analysis = result.get("ai_analysis", {})
            detailed = ai_analysis.get("detailed_analysis", {})
            if isinstance(detailed, dict) and any(
                isinstance(detailed.get(field, []), list) and len(detailed.get(field, [])) > 0 
                for field in ['visual_changes', 'functional_impact', 'technical_correlation']
            ):
                detailed_analysis_count += 1
                
            # Error tracking
            if result.get("processing_status") == "error":
                error_count += 1
        
        indicators["data_completeness"] = round(complete_data_count / total_urls, 3)
        indicators["screenshot_coverage"] = round(screenshot_coverage_count / total_urls, 3)
        indicators["detailed_analysis_coverage"] = round(detailed_analysis_count / total_urls, 3)
        indicators["error_rate"] = round(error_count / total_urls, 3)
        
        return indicators
    
    def _generate_validation_warnings(self, all_url_results: List[Dict]) -> List[str]:
        """Generate warnings based on analysis validation"""
        
        warnings = []
        
        if not all_url_results:
            warnings.append("No URL results available for validation")
            return warnings
        
        total_urls = len(all_url_results)
        error_count = sum(1 for r in all_url_results if r.get("processing_status") == "error")
        success_count = total_urls - error_count
        
        # High error rate warning
        if error_count / total_urls > 0.2:  # More than 20% errors
            warnings.append(f"High error rate: {error_count}/{total_urls} URLs failed processing")
        
        # Low confidence warnings
        low_confidence_count = 0
        for result in all_url_results:
            ai_analysis = result.get("ai_analysis", {})
            confidence = ai_analysis.get("confidence_score", 1.0)
            if isinstance(confidence, (int, float)) and confidence < 0.6:
                low_confidence_count += 1
        
        if low_confidence_count > 0 and success_count > 0:
            low_confidence_rate = low_confidence_count / success_count
            if low_confidence_rate > 0.3:
                warnings.append(f"Multiple URLs with low AI confidence: {low_confidence_count}/{success_count} successful analyses")
        
        # Screenshot coverage warning
        no_screenshot_count = sum(1 for r in all_url_results if not r.get("screenshots_available"))
        if no_screenshot_count > 0:
            warnings.append(f"Missing screenshots for {no_screenshot_count}/{total_urls} URLs")
        
        # Data quality warning
        incomplete_data_count = 0
        for result in all_url_results:
            structured_data = result.get("structured_data", {})
            if not all(key in structured_data for key in ['html_changes', 'css_changes', 'js_changes']):
                incomplete_data_count += 1
                
        if incomplete_data_count > total_urls * 0.25:  # More than 25% incomplete
            warnings.append(f"Incomplete structured data for {incomplete_data_count}/{total_urls} URLs")
        
        return warnings

    async def generate(self, comparisons: List[Dict], output_dir: Path) -> Path:
        """Generate HTML report from comparison results, including AI analysis."""
        report_path = output_dir / "report.html"
        images_dir = output_dir / "images"
        images_dir.mkdir(exist_ok=True)

        processed_comparisons = []
        for comp in comparisons:
            # Process paths and copy images for the report
            url_name = comp["url"]
            report_subdir = output_dir / url_name

            # Handle screenshot paths from the comparison data
            screenshot_comp = comp.get("comparison", {}).get("screenshots", {})
            if screenshot_comp.get("diff_image_path"):
                diff_img_path = Path(screenshot_comp["diff_image_path"])
                if diff_img_path.exists():
                    shutil.copy(diff_img_path, images_dir / f"diff_{url_name}.png")
                    screenshot_comp["diff_image_path"] = f"images/diff_{url_name}.png"

            # Copy baseline and current images for reference
            baseline_screenshot_path = Path(comp.get("comparison", {}).get("screenshots", {}).get("baseline_path", ""))
            current_screenshot_path = Path(comp.get("comparison", {}).get("screenshots", {}).get("current_path", ""))
            if baseline_screenshot_path and baseline_screenshot_path.exists():
                 shutil.copy(baseline_screenshot_path, images_dir / f"baseline_{url_name}.png")
                 comp["baseline_screenshot_path"] = f"images/baseline_{url_name}.png"
            if current_screenshot_path and current_screenshot_path.exists():
                 shutil.copy(current_screenshot_path, images_dir / f"current_{url_name}.png")
                 comp["current_screenshot_path"] = f"images/current_{url_name}.png"


            # AI Analysis moved here
            ai_analysis = {}
            ssim_score = screenshot_comp.get("ssim_score", 1.0)
            if ssim_score < 1.0:
                logger.info(f"Screenshots for {url_name} differ (SSIM: {ssim_score:.4f}). Sending to AI for analysis.")
                try:
                    baseline_b64 = base64.b64encode(baseline_screenshot_path.read_bytes()).decode()
                    current_b64 = base64.b64encode(current_screenshot_path.read_bytes()).decode()

                    response = await self.client.post(
                        f"{self.gemini_url}/api/compare",
                        json={
                            "url": url_name,
                            "baseline_screenshot": baseline_b64,
                            "current_screenshot": current_b64,
                        }
                    )
                    response.raise_for_status()
                    ai_analysis = response.json()
                except Exception as e:
                    logger.error(f"Failed to get AI analysis for {url_name}: {e}")
                    ai_analysis = {"error": "Failed to get AI analysis", "details": str(e)}
            else:
                ai_analysis = {"status": "skipped", "reason": "Screenshots are identical."}

            comp["ai_analysis"] = ai_analysis
            comp["severity"] = ai_analysis.get("severity", "SAFE" if ssim_score == 1.0 else "UNKNOWN")
            processed_comparisons.append(comp)

        # Group diffs by severity
        critical = [d for d in processed_comparisons if d.get("severity") == "CRITICAL"]
        warnings = [d for d in processed_comparisons if d.get("severity") == "WARNING"]
        safe = [d for d in processed_comparisons if d.get("severity") == "SAFE"]
        errors = [d for d in processed_comparisons if d.get("severity") == "ERROR"]

        html = self._render_template({
            "timestamp": settings.get_current_datetime(),
            "total_pages": len(processed_comparisons),
            "critical": critical,
            "warnings": warnings,
            "safe": safe,
            "errors": errors,
        })

        report_path.write_text(html, encoding="utf-8")
        return report_path

    def create_enhanced_template(self) -> str:
        """Enhanced HTML template with comprehensive AI analysis integration"""
        return '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI-Powered UI Regression Analysis Report - {{ timestamp }}</title>
    <style>
        * { box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; 
            margin: 0; padding: 20px; background: #fafafa; line-height: 1.6; 
        }
        
        .container { max-width: 1400px; margin: 0 auto; }
        
        .header { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
            color: white; padding: 30px; border-radius: 12px; margin-bottom: 30px; 
            box-shadow: 0 4px 20px rgba(0,0,0,0.1);
        }
        .header h1 { margin: 0 0 10px 0; font-size: 2.5em; font-weight: 300; }
        .header .subtitle { opacity: 0.9; font-size: 1.1em; margin: 5px 0; }
        .header .meta { opacity: 0.8; font-size: 0.9em; margin-top: 15px; }
        
        .executive-summary { 
            background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.05); border-left: 4px solid #667eea;
        }
        .executive-summary h2 { margin-top: 0; color: #333; }
        
        .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }
        .stat-card { 
            background: white; padding: 20px; border-radius: 10px; text-align: center; 
            box-shadow: 0 2px 8px rgba(0,0,0,0.08); transition: transform 0.2s;
        }
        .stat-card:hover { transform: translateY(-2px); }
        .stat-number { font-size: 2.2em; font-weight: bold; margin: 10px 0; }
        .stat-label { color: #666; font-size: 0.9em; text-transform: uppercase; letter-spacing: 1px; }
        
        .critical .stat-number { color: #dc3545; }
        .warning .stat-number { color: #fd7e14; }
        .safe .stat-number { color: #28a745; }
        .error .stat-number { color: #6f42c1; }
        
        .confidence-meter { 
            background: white; padding: 20px; border-radius: 10px; margin: 20px 0;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }
        .confidence-bar { 
            height: 8px; background: #e9ecef; border-radius: 4px; margin: 10px 0; position: relative;
        }
        .confidence-fill { 
            height: 100%; border-radius: 4px; transition: width 0.3s;
            background: linear-gradient(90deg, #28a745, #20c997, #17a2b8);
        }
        
        .patterns-section, .recommendations-section { 
            background: white; padding: 25px; border-radius: 12px; margin-bottom: 25px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.05);
        }
        
        .section-title { 
            color: #333; margin-bottom: 20px; font-size: 1.4em; font-weight: 600;
            border-bottom: 2px solid #e9ecef; padding-bottom: 8px;
        }
        
        .url-card { 
            background: white; border: 1px solid #e0e0e0; border-radius: 10px; 
            margin-bottom: 20px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
        .url-header { 
            padding: 20px; background: #f8f9fa; border-bottom: 1px solid #e0e0e0;
            display: flex; justify-content: space-between; align-items: center;
        }
        .url-title { margin: 0; color: #333; font-size: 1.2em; }
        .severity-badge { 
            padding: 6px 12px; border-radius: 20px; font-size: 0.8em; font-weight: bold;
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .severity-critical { background: #f8d7da; color: #721c24; }
        .severity-warning { background: #fff3cd; color: #856404; }
        .severity-safe { background: #d4edda; color: #155724; }
        .severity-error { background: #e2e3e5; color: #383d41; }
        
        .url-content { padding: 20px; }
        
        .ai-analysis { 
            background: linear-gradient(135deg, #f8f9ff 0%, #fff 100%); 
            padding: 20px; border-radius: 8px; margin-bottom: 20px;
            border-left: 4px solid #667eea;
        }
        .ai-analysis h4 { color: #667eea; margin-top: 0; display: flex; align-items: center; }
        .ai-analysis h4::before { content: "🤖"; margin-right: 8px; }
        
        .analysis-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin: 15px 0; }
        .analysis-item { background: white; padding: 15px; border-radius: 6px; }
        .analysis-item h5 { margin-top: 0; color: #555; font-size: 0.9em; text-transform: uppercase; letter-spacing: 0.5px; }
        .analysis-item ul { margin: 5px 0; padding-left: 15px; }
        .analysis-item li { margin-bottom: 5px; color: #666; font-size: 0.9em; }
        
        .recommendations { 
            background: #fff8e1; padding: 15px; border-radius: 8px; margin-top: 15px;
            border-left: 4px solid #ffc107;
        }
        .recommendations h5 { color: #e65100; margin-top: 0; }
        
        .screenshots-section { margin-top: 25px; }
        .screenshots-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
        .screenshot-card { background: white; padding: 15px; border-radius: 8px; text-align: center; }
        .screenshot-card img { 
            max-width: 100%; height: auto; border-radius: 6px; 
            box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-top: 10px;
        }
        .screenshot-label { font-weight: 600; color: #555; margin-bottom: 5px; }
        
        .technical-details { 
            background: #f8f9fa; padding: 15px; border-radius: 8px; margin-top: 20px;
        }
        .technical-details summary { 
            cursor: pointer; font-weight: 600; color: #666; padding: 5px 0;
        }
        .technical-details[open] summary { margin-bottom: 10px; }
        
        .code-snippet { 
            background: #2d3748; color: #e2e8f0; padding: 15px; border-radius: 6px; 
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace; font-size: 0.85em; 
            overflow-x: auto; margin: 10px 0;
        }
        
        .pattern-item { 
            background: white; padding: 15px; border-radius: 8px; margin: 10px 0;
            border-left: 3px solid #17a2b8;
        }
        
        .recommendation-category { margin: 15px 0; }
        .recommendation-category h4 { color: #495057; margin-bottom: 8px; }
        .recommendation-list { list-style: none; padding: 0; }
        .recommendation-list li { 
            padding: 8px 12px; margin: 5px 0; background: #f8f9fa; 
            border-radius: 6px; border-left: 3px solid #28a745;
        }
        .recommendation-list li::before { content: "→"; color: #28a745; margin-right: 8px; font-weight: bold; }
        
        @media (max-width: 768px) {
            .analysis-grid, .screenshots-grid { grid-template-columns: 1fr; }
            .summary-grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 AI-Powered UI Regression Analysis</h1>
            <div class="subtitle">Comprehensive Cross-URL Pattern Detection & Analysis</div>
            <div class="meta">
                Generated: {{ timestamp }} | 
                URLs Analyzed: {{ aggregation.summary.total_urls_analyzed }} | 
                Success Rate: {{ (aggregation.confidence_metrics.success_rate * 100)|round(1) }}%
            </div>
        </div>

        <div class="executive-summary">
            <h2>📊 Executive Summary</h2>
            <div class="summary-grid">
                <div class="stat-card critical">
                    <div class="stat-number">{{ aggregation.summary.critical_issues }}</div>
                    <div class="stat-label">Critical Issues</div>
                </div>
                <div class="stat-card warning">
                    <div class="stat-number">{{ aggregation.summary.warnings }}</div>
                    <div class="stat-label">Warnings</div>
                </div>
                <div class="stat-card safe">
                    <div class="stat-number">{{ aggregation.summary.safe_changes }}</div>
                    <div class="stat-label">Safe Changes</div>
                </div>
                <div class="stat-card error">
                    <div class="stat-number">{{ aggregation.summary.errors }}</div>
                    <div class="stat-label">Processing Errors</div>
                </div>
            </div>

            <div class="confidence-meter">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <strong>AI Analysis Confidence</strong>
                    <span>{{ (aggregation.confidence_metrics.average_confidence * 100)|round(1) }}%</span>
                </div>
                <div class="confidence-bar">
                    <div class="confidence-fill" style="width: {{ (aggregation.confidence_metrics.average_confidence * 100)|round(1) }}%"></div>
                </div>
                <small>Range: {{ (aggregation.confidence_metrics.min_confidence * 100)|round(1) }}% - {{ (aggregation.confidence_metrics.max_confidence * 100)|round(1) }}%</small>
            </div>
        </div>

        {% if aggregation.patterns %}
        <div class="patterns-section">
            <h2 class="section-title">🔍 Cross-URL Patterns Detected</h2>
            
            {% if aggregation.patterns.common_html_changes.total_changes > 0 %}
            <div class="pattern-item">
                <h4>HTML Changes Analysis</h4>
                <p>Total changes across all URLs: <strong>{{ aggregation.patterns.common_html_changes.total_changes }}</strong></p>
                {% if aggregation.patterns.common_html_changes.change_types %}
                <p>Most common change types:</p>
                <ul>
                    {% for change_type, count in aggregation.patterns.common_html_changes.change_types.items() %}
                    <li><strong>{{ change_type }}</strong>: {{ count }} occurrences</li>
                    {% endfor %}
                </ul>
                {% endif %}
            </div>
            {% endif %}

            {% if aggregation.patterns.business_impact_distribution %}
            <div class="pattern-item">
                <h4>Business Impact Distribution</h4>
                {% for impact, count in aggregation.patterns.business_impact_distribution.items() %}
                <div style="margin: 5px 0;">
                    <strong>{{ impact }}</strong>: {{ count }} URL(s)
                </div>
                {% endfor %}
            </div>
            {% endif %}

            {% if aggregation.patterns.recurring_issues %}
            <div class="pattern-item">
                <h4>Recurring Issues</h4>
                {% for issue in aggregation.patterns.recurring_issues %}
                <div style="margin: 8px 0;">
                    <strong>"{{ issue.keyword }}"</strong> mentioned {{ issue.frequency }} times across {{ issue.urls_affected }} URL(s)
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        {% endif %}

        {% if aggregation.recommendations %}
        <div class="recommendations-section">
            <h2 class="section-title">💡 Global Recommendations</h2>
            
            {% if aggregation.recommendations.immediate_actions %}
            <div class="recommendation-category">
                <h4>🚨 Immediate Actions</h4>
                <ul class="recommendation-list">
                    {% for action in aggregation.recommendations.immediate_actions %}
                    <li>{{ action }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}

            {% if aggregation.recommendations.strategic_actions %}
            <div class="recommendation-category">
                <h4>📋 Strategic Actions</h4>
                <ul class="recommendation-list">
                    {% for action in aggregation.recommendations.strategic_actions %}
                    <li>{{ action }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}

            {% if aggregation.recommendations.process_improvements %}
            <div class="recommendation-category">
                <h4>⚙️ Process Improvements</h4>
                <ul class="recommendation-list">
                    {% for improvement in aggregation.recommendations.process_improvements %}
                    <li>{{ improvement }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}

            {% if aggregation.recommendations.monitoring_suggestions %}
            <div class="recommendation-category">
                <h4>📊 Monitoring Suggestions</h4>
                <ul class="recommendation-list">
                    {% for suggestion in aggregation.recommendations.monitoring_suggestions %}
                    <li>{{ suggestion }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}
        </div>
        {% endif %}

        <h2 class="section-title">🌐 Individual URL Analysis</h2>
        {% for url_result in url_results %}
        <div class="url-card">
            <div class="url-header">
                <h3 class="url-title">{{ url_result.url }}</h3>
                <span class="severity-badge severity-{{ url_result.ai_analysis.overall_severity|lower }}">
                    {{ url_result.ai_analysis.overall_severity }}
                </span>
            </div>
            <div class="url-content">
                {% if url_result.ai_analysis.analysis_type != "no_changes_detected" %}
                <div class="ai-analysis">
                    <h4>AI Analysis Results</h4>
                    {% if url_result.ai_analysis.detailed_analysis %}
                    <div class="analysis-grid">
                        {% if url_result.ai_analysis.detailed_analysis.visual_changes %}
                        <div class="analysis-item">
                            <h5>Visual Changes</h5>
                            <ul>
                                {% for change in url_result.ai_analysis.detailed_analysis.visual_changes %}
                                <li>{{ change }}</li>
                                {% endfor %}
                            </ul>
                        </div>
                        {% endif %}

                        {% if url_result.ai_analysis.detailed_analysis.functional_impact %}
                        <div class="analysis-item">
                            <h5>Functional Impact</h5>
                            <ul>
                                {% for impact in url_result.ai_analysis.detailed_analysis.functional_impact %}
                                <li>{{ impact }}</li>
                                {% endfor %}
                            </ul>
                        </div>
                        {% endif %}

                        {% if url_result.ai_analysis.detailed_analysis.technical_correlation %}
                        <div class="analysis-item">
                            <h5>Technical Correlation</h5>
                            <ul>
                                {% for correlation in url_result.ai_analysis.detailed_analysis.technical_correlation %}
                                <li>{{ correlation }}</li>
                                {% endfor %}
                            </ul>
                        </div>
                        {% endif %}
                    </div>
                    {% endif %}

                    {% if url_result.ai_analysis.recommendations %}
                    <div class="recommendations">
                        <h5>🎯 AI Recommendations</h5>
                        {% if url_result.ai_analysis.recommendations.immediate_actions %}
                        <strong>Immediate:</strong> {{ url_result.ai_analysis.recommendations.immediate_actions|join(', ') }}<br>
                        {% endif %}
                        {% if url_result.ai_analysis.recommendations.acceptance_criteria %}
                        <strong>Acceptance:</strong> {{ url_result.ai_analysis.recommendations.acceptance_criteria }}
                        {% endif %}
                    </div>
                    {% endif %}

                    <div style="margin-top: 15px;">
                        <strong>Business Impact:</strong> {{ url_result.ai_analysis.business_impact }} | 
                        <strong>Confidence:</strong> {{ (url_result.ai_analysis.confidence_score * 100)|round(1) }}%
                        {% if url_result.ai_analysis.reasoning %}
                        | <strong>Reasoning:</strong> {{ url_result.ai_analysis.reasoning }}
                        {% endif %}
                    </div>
                </div>

                {% if url_result.screenshots_available %}
                <div class="screenshots-section">
                    <h4>Visual Comparison</h4>
                    <div class="screenshots-grid">
                        {% for screenshot_type in url_result.screenshots_available %}
                        <div class="screenshot-card">
                            <div class="screenshot-label">{{ screenshot_type|title }}</div>
                            <img src="{{ url_result.report_path.name }}/screenshots/{{ screenshot_type }}.png" 
                                 alt="{{ screenshot_type }} screenshot" loading="lazy">
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}

                {% else %}
                <div class="ai-analysis">
                    <h4>No Changes Detected</h4>
                    <p>{{ url_result.ai_analysis.detailed_analysis.technical_changes[0] if url_result.ai_analysis.detailed_analysis.technical_changes else "This URL has no detected changes." }}</p>
                    <div style="margin-top: 10px;">
                        <strong>Confidence:</strong> {{ (url_result.ai_analysis.confidence_score * 100)|round(1) }}%
                    </div>
                </div>
                {% endif %}

                <details class="technical-details">
                    <summary>🔧 Technical Details</summary>
                    {% if url_result.structured_data and url_result.structured_data.metadata %}
                    <div style="margin-top: 10px;">
                        <strong>Analysis Metadata:</strong><br>
                        Files processed: {{ url_result.structured_data.metadata.files_loaded }}<br>
                        Data directory: {{ url_result.structured_data.metadata.diffs_directory }}<br>
                        Timestamp: {{ url_result.structured_data.metadata.timestamp }}
                    </div>
                    {% endif %}
                    
                    {% if url_result.ai_analysis.analysis_metadata %}
                    <div style="margin-top: 10px;">
                        <strong>AI Analysis Metadata:</strong><br>
                        Request type: {{ url_result.ai_analysis.analysis_metadata.request_type }}<br>
                        Data sources: {{ url_result.ai_analysis.analysis_metadata.data_sources|join(', ') }}<br>
                        {% if url_result.ai_analysis.analysis_metadata.total_changes_analyzed %}
                        Changes analyzed: {{ url_result.ai_analysis.analysis_metadata.total_changes_analyzed }}
                        {% endif %}
                    </div>
                    {% endif %}
                </details>
            </div>
        </div>
        {% endfor %}

        <div style="text-align: center; margin-top: 40px; padding: 20px; color: #666; font-size: 0.9em;">
            Report generated by AI-Powered UI Regression Analysis System<br>
            <small>Analysis confidence represents AI model certainty in assessment accuracy</small>
        </div>
    </div>
</body>
</html>
'''
    
    def _render_template(self, data: Dict) -> str:
        """Render enhanced HTML template with comprehensive AI analysis data"""
        template = Template(self.create_enhanced_template())
        return template.render(data)
    
    async def generate_enhanced_report(self, report_date: str) -> Path:
        """Generate comprehensive HTML report with AI insights and cross-URL analysis"""
        logger.info(f"Generating enhanced report for {report_date}")
        
        report_dir = Path("data/report") / report_date
        if not report_dir.exists():
            raise ValueError(f"No report data found for {report_date}")
        
        # Load all processed URL results
        all_url_results = []
        for url_dir in report_dir.iterdir():
            if url_dir.is_dir():
                ai_analysis_file = url_dir / "ai_analysis.json"
                structured_data_file = url_dir / "structured_data.json"
                
                if ai_analysis_file.exists():
                    try:
                        ai_analysis = json.loads(ai_analysis_file.read_text(encoding='utf-8'))
                        structured_data = {}
                        if structured_data_file.exists():
                            structured_data = json.loads(structured_data_file.read_text(encoding='utf-8'))
                        
                        all_url_results.append({
                            "url": url_dir.name,
                            "ai_analysis": ai_analysis,
                            "structured_data": structured_data,
                            "report_path": url_dir,
                            "processing_status": "success" if ai_analysis.get("overall_severity") != "ERROR" else "error",
                            "screenshots_available": self._get_available_screenshots(url_dir / "screenshots")
                        })
                    except Exception as e:
                        logger.warning(f"Failed to load analysis for {url_dir.name}: {e}")
        
        if not all_url_results:
            raise ValueError(f"No processed URL results found for {report_date}")
        
        # Load or generate aggregated analysis
        aggregated_file = report_dir / "aggregated_analysis.json"
        if aggregated_file.exists():
            logger.info("Loading existing aggregated analysis")
            aggregated_analysis = json.loads(aggregated_file.read_text(encoding='utf-8'))
        else:
            logger.info("Generating new aggregated analysis")
            aggregated_analysis = self.aggregate_analyses(all_url_results)
            # Save for future reference
            aggregated_file.write_text(
                json.dumps(aggregated_analysis, indent=2, default=str),
                encoding='utf-8'
            )
        
        # Prepare template data
        template_data = {
            "timestamp": settings.get_current_datetime(),
            "report_date": report_date,
            "aggregation": aggregated_analysis,
            "url_results": all_url_results,
            "total_urls": len(all_url_results),
            "has_critical": aggregated_analysis["summary"]["critical_issues"] > 0,
            "has_warnings": aggregated_analysis["summary"]["warnings"] > 0,
            "has_errors": aggregated_analysis["summary"]["errors"] > 0,
            "confidence_level": "high" if aggregated_analysis.get("confidence_metrics", {}).get("average_confidence", 0) >= 0.8 else "medium",
            "system_status": self._determine_system_status(aggregated_analysis)
        }
        
        # Generate HTML report
        html_content = self._render_template(template_data)
        
        # Save enhanced report
        enhanced_report_path = report_dir / "enhanced_analysis_report.html"
        enhanced_report_path.write_text(html_content, encoding='utf-8')
        
        logger.info(f"Enhanced report generated: {enhanced_report_path}")
        return enhanced_report_path
    
    def _get_available_screenshots(self, screenshots_dir: Path) -> List[str]:
        """Get list of available screenshot types in the screenshots directory"""
        if not screenshots_dir.exists():
            return []
        
        available = []
        for screenshot_type in ['baseline', 'current', 'visual_diff']:
            screenshot_path = screenshots_dir / f"{screenshot_type}.png"
            if screenshot_path.exists():
                available.append(screenshot_type)
        return available
    
    def _determine_system_status(self, aggregated_analysis: Dict) -> str:
        """Determine overall system status based on aggregated analysis"""
        summary = aggregated_analysis.get("summary", {})
        
        if summary.get("critical_issues", 0) > 0:
            return "critical"
        elif summary.get("warnings", 0) > 0:
            return "warning"
        elif summary.get("errors", 0) > 0:
            return "error"
        elif summary.get("urls_with_changes", 0) > 0:
            return "changes"
        else:
            return "stable"
