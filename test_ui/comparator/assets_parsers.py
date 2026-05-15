"""CSS/JS parsing and lightweight heuristic helpers."""

from __future__ import annotations

import re

# Categories used by _assess_css_impact.
LAYOUT_PROPS = (
    "width",
    "height",
    "margin",
    "padding",
    "display",
    "position",
    "float",
    "flex",
    "grid",
)
HIGH_IMPACT_SELECTORS = ("body", "html", ".header", ".footer", ".nav", ".main")


# ---------------------------------------------------------------------------
# CSS / JS heuristic parsers
# ---------------------------------------------------------------------------


def parse_css_rules(css_content: str) -> dict[str, dict[str, str]]:
    """Naive selector → {prop: value} extraction. Misses nested rules and at-rules.

    Backward-compat wrapper over the indexed parser; last occurrence wins so
    behaviour is preserved for existing callers (e.g. ``analyze_new_file``).
    """
    indexed = parse_css_rules_indexed(css_content)
    return {sel: occs[-1] for sel, occs in indexed.items() if occs}


def parse_css_rules_indexed(css_content: str) -> dict[str, list[dict[str, str]]]:
    """Naive selector → list[{prop: value}] extraction.

    Unlike ``parse_css_rules`` this keeps **all** occurrences of a selector,
    so a duplicate like ``body`` appearing twice (once with a mutated
    ``color`` and once with the original ``min-width``) doesn't silently
    overwrite itself. This was the root cause of the empty-``content_changes``
    bug on site 14 of audit 01KRB5GSSM3J76H9Y2MPTZWPS4.

    Still misses nested at-rules (``@media``, ``@keyframes``) because the
    regex doesn't balance braces. Those differences fall back to a raw-
    content change record in ``analyze_css_content_changes``.
    """
    rules: dict[str, list[dict[str, str]]] = {}
    pattern = r"([^{}]+)\s*{\s*([^{}]+)\s*}"
    for selector, properties in re.findall(pattern, css_content):
        selector = selector.strip()
        props: dict[str, str] = {}
        for prop in properties.split(";"):
            if ":" in prop:
                key, value = prop.split(":", 1)
                props[key.strip()] = value.strip()
        if props:
            rules.setdefault(selector, []).append(props)
    return rules


def extract_js_functions(js_content: str) -> dict[str, str]:
    """Extract top-level function declarations + assigned anonymous functions.

    Doesn't handle arrow functions, async functions, or nested braces beyond
    one level. Intentional - pre-A.3 behavior preserved.
    """
    functions: dict[str, str] = {}
    patterns = (
        r"function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
        r"const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
        r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:\s*function\s*\([^)]*\)\s*{([^}]*(?:{[^}]*}[^}]*)*)}",
    )
    for pattern in patterns:
        for match in re.findall(pattern, js_content, re.DOTALL):
            if len(match) >= 2:
                func_name, func_body = match[0], match[1]
                functions[func_name] = f"function {func_name}() {{{func_body}}}"
    return functions


def extract_js_variables(js_content: str) -> dict[str, str]:
    """Names of var/let/const declarations. Value is always the literal `'declared'`."""
    variables: dict[str, str] = {}
    for pattern in (
        r"var\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
        r"let\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
        r"const\s+([a-zA-Z_$][a-zA-Z0-9_$]*)",
    ):
        for var_name in re.findall(pattern, js_content):
            variables[var_name] = "declared"
    return variables


def assess_css_impact(selector: str, properties: dict[str, str]) -> str:
    """Heuristic: layout-affecting props → high, top-level selectors → medium."""
    if any(prop in properties for prop in LAYOUT_PROPS):
        return "high"
    if any(selector.startswith(sel) for sel in HIGH_IMPACT_SELECTORS):
        return "medium"
    return "low"


def _format_rule(selector: str, props: dict[str, str]) -> str:
    inner = "; ".join(f"{k}: {v}" for k, v in props.items())
    return f"{selector} {{\n  {inner}\n}}"


def _scan_js_security_indicators(content: str) -> list[str]:
    """Extract security-relevant code snippets from raw JS content.

    The regex-based function parser only sees top-level function
    declarations. Appended attack vectors (eval, fetch, document.write,
    dynamic import, sendBeacon) that are NOT wrapped in functions are
    invisible to it. This helper scans the raw text for a curated set
    of dangerous patterns and returns matching snippets so the AI can
    see them even when the function-level diff comes up empty.

    Snippets are capped at 200 chars to avoid prompt bloat.
    """
    patterns = (
        r"fetch\s*\(\s*['\"]https://attacker\.example[^'\"]*['\"]",
        r"eval\s*\(",
        r"document\.write\s*\(",
        r"insertAdjacentHTML\s*\(",
        r"navigator\.sendBeacon\s*\(",
        r"import\s*\(\s*['\"]https://attacker\.example[^'\"]*['\"]",
        r"setTimeout\s*\(\s*['\"]",
        r"document\.cookie\s*=",
        r"localStorage\.setItem\s*\(",
    )
    found: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, content, re.IGNORECASE):
            snippet = content[m.start() : m.start() + 200]
            if len(snippet) >= 200:
                snippet = snippet[:197] + "..."
            found.append(snippet)
    return found

__all__ = [
    "parse_css_rules",
    "parse_css_rules_indexed",
    "extract_js_functions",
    "extract_js_variables",
    "assess_css_impact",
    "_format_rule",
    "_scan_js_security_indicators",
]
