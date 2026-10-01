#!/usr/bin/env python3
"""
validate_schema.py - Extract and sanity-check JSON-LD structured data.

Input can be:
  * a URL                      (the page is fetched and every JSON-LD block extracted)
  * a local .html/.htm file    (same, without fetching)
  * a local .json/.jsonld file (treated as one JSON-LD block)

For every block it:
  1. checks the JSON parses (and reports the line/column of syntax errors)
  2. walks the whole graph, including @graph arrays and nested objects
  3. checks each typed entity against a list of required and recommended
     properties (based on Schema.org and Google rich result documentation)

Results are printed as a readable report and can also be saved as CSV.
The exit code is 1 if any errors were found, so it can be used in CI.

Usage:
    python validate_schema.py https://example.com/
    python validate_schema.py templates/product.json
    python validate_schema.py examples/sample-page.html --csv report.csv
    python validate_schema.py templates/*.json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Property rules per type.
#   required:    missing -> ERROR   (entity is unlikely to be valid / eligible)
#   recommended: missing -> WARNING (entity works, but is less complete)
#   one_of:      at least one of each group must be present -> ERROR
# These are deliberately pragmatic. Always confirm eligibility with Google's
# Rich Results Test for your specific use case.
# ---------------------------------------------------------------------------
RULES: dict[str, dict[str, list]] = {
    "Organization": {
        "required": ["name", "url"],
        "recommended": ["logo", "sameAs", "description", "address", "contactPoint"],
    },
    "LocalBusiness": {
        "required": ["name", "address"],
        "recommended": ["url", "telephone", "image", "geo", "openingHoursSpecification", "priceRange"],
    },
    "PostalAddress": {
        "required": [],
        "recommended": ["streetAddress", "addressLocality", "addressCountry"],
    },
    "Product": {
        "required": ["name"],
        "one_of": [["offers", "review", "aggregateRating"]],
        "recommended": ["image", "description", "sku", "brand", "offers"],
    },
    "Offer": {
        "required": ["priceCurrency"],
        "one_of": [["price", "priceSpecification"]],
        "recommended": ["availability", "url", "priceValidUntil", "itemCondition"],
    },
    "AggregateRating": {
        "required": ["ratingValue"],
        "one_of": [["ratingCount", "reviewCount"]],
        "recommended": ["bestRating"],
    },
    "FAQPage": {"required": ["mainEntity"], "recommended": []},
    "Question": {"required": ["name", "acceptedAnswer"], "recommended": []},
    "Answer": {"required": ["text"], "recommended": []},
    "BreadcrumbList": {"required": ["itemListElement"], "recommended": []},
    "ListItem": {"required": ["position", "name"], "recommended": ["item"]},
    "Article": {
        "required": ["headline"],
        "recommended": ["image", "datePublished", "dateModified", "author", "publisher"],
    },
    "Person": {"required": ["name"], "recommended": ["url"]},
    "WebSite": {"required": ["name", "url"], "recommended": ["potentialAction", "publisher"]},
    "SearchAction": {"required": ["target", "query-input"], "recommended": []},
}

# Common subtypes mapped to the base type whose rules they should follow.
SUBTYPES: dict[str, str] = {
    **{t: "Article" for t in ("NewsArticle", "BlogPosting", "TechArticle", "Report")},
    **{t: "LocalBusiness" for t in (
        "Restaurant", "Store", "Dentist", "MedicalClinic", "AutoRepair", "HomeAndConstructionBusiness",
        "ProfessionalService", "LegalService", "RealEstateAgent", "Hotel", "ElectronicsStore",
        "HVACBusiness", "Plumber", "Electrician", "CleaningService" )},
    **{t: "Organization" for t in ("Corporation", "NGO", "EducationalOrganization", "OnlineBusiness")},
    "ProductGroup": "Product",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass
class Finding:
    source: str
    block: int
    path: str
    entity_type: str
    level: str  # ERROR | WARNING | OK
    message: str


# ---------------------------------------------------------------------------
# Loading and extraction
# ---------------------------------------------------------------------------
def load_source(source: str, timeout: float) -> tuple[str, str]:
    """Return (content, kind) where kind is 'html' or 'json'."""
    if source.startswith(("http://", "https://")):
        import requests  # Imported lazily so local files work without requests installed.

        resp = requests.get(source, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; SchemaValidator/1.0)"})
        resp.raise_for_status()
        return resp.text, "html"
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    return text, "json" if path.suffix.lower() in (".json", ".jsonld") else "html"


def extract_blocks(html: str) -> list[str]:
    """Return the raw text of every <script type="application/ld+json"> block."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # Fallback regex keeps the script usable without bs4.
        pattern = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
        return [m.strip() for m in pattern.findall(html)]
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", attrs={"type": lambda t: t and "ld+json" in t.lower()})
    return [(s.string or s.get_text() or "").strip() for s in scripts]


# ---------------------------------------------------------------------------
# Graph walking and checks
# ---------------------------------------------------------------------------
def types_of(node: dict) -> list[str]:
    raw = node.get("@type", [])
    return [raw] if isinstance(raw, str) else [t for t in raw if isinstance(t, str)]


def iter_entities(data: Any, path: str = "$") -> Iterator[tuple[str, dict]]:
    """Yield (json_path, node) for every dict that has an @type, at any depth."""
    if isinstance(data, list):
        for i, item in enumerate(data):
            yield from iter_entities(item, f"{path}[{i}]")
    elif isinstance(data, dict):
        if "@type" in data:
            yield path, data
        for key, value in data.items():
            if key != "@context":
                yield from iter_entities(value, f"{path}.{key}")


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0)


def is_reference(node: dict) -> bool:
    """A node like {"@id": "..."} only points to an entity defined elsewhere."""
    return set(node) <= {"@id", "@type"} and "@id" in node


def check_entity(node: dict, base_type: str) -> list[tuple[str, str]]:
    """Return (level, message) pairs for a single entity."""
    rules = RULES[base_type]
    out: list[tuple[str, str]] = []
    for prop in rules.get("required", []):
        if is_empty(node.get(prop)):
            out.append(("ERROR", f"missing required property '{prop}'"))
    for group in rules.get("one_of", []):
        if all(is_empty(node.get(p)) for p in group):
            out.append(("ERROR", f"needs at least one of: {', '.join(group)}"))
    for prop in rules.get("recommended", []):
        if is_empty(node.get(prop)):
            out.append(("WARNING", f"missing recommended property '{prop}'"))

    # A few value-level checks that catch common real-world mistakes.
    for prop in ("datePublished", "dateModified", "priceValidUntil"):
        if isinstance(node.get(prop), str) and not DATE_RE.match(node[prop]):
            out.append(("ERROR", f"'{prop}' is not an ISO 8601 date: {node[prop]!r}"))
    if isinstance(node.get("priceCurrency"), str) and not CURRENCY_RE.match(node["priceCurrency"]):
        out.append(("ERROR", f"priceCurrency should be an ISO 4217 code (e.g. AED, SAR): {node['priceCurrency']!r}"))
    if isinstance(node.get("price"), str) and not re.match(r"^\d+(\.\d+)?$", node["price"]):
        out.append(("ERROR", f"price should be a plain number without symbols: {node['price']!r}"))
    for prop in ("url", "logo", "image", "item"):
        value = node.get(prop)
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, str) and v and not v.startswith(("http://", "https://")):
                out.append(("WARNING", f"'{prop}' should be an absolute URL: {v!r}"))
    if base_type == "SearchAction":
        target = node.get("target")
        url_template = target.get("urlTemplate", "") if isinstance(target, dict) else str(target or "")
        if "{search_term_string}" not in url_template:
            out.append(("ERROR", "SearchAction target must contain {search_term_string}"))
    if base_type == "BreadcrumbList" and isinstance(node.get("itemListElement"), list):
        positions = [i.get("position") for i in node["itemListElement"] if isinstance(i, dict)]
        if positions != list(range(1, len(positions) + 1)):
            out.append(("ERROR", f"ListItem positions should be 1..n in order, got {positions}"))
    if base_type == "FAQPage" and isinstance(node.get("mainEntity"), list) and not node["mainEntity"]:
        out.append(("ERROR", "FAQPage has no questions"))
    return out


def validate_block(source: str, index: int, raw: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [Finding(source, index, "$", "-", "ERROR",
                        f"JSON syntax error at line {exc.lineno}, column {exc.colno}: {exc.msg}")]

    roots = data if isinstance(data, list) else [data]
    for root in roots:
        if isinstance(root, dict) and "@context" in root and "schema.org" not in str(root["@context"]):
            findings.append(Finding(source, index, "$", "-", "WARNING",
                                    f"@context is not schema.org: {root['@context']!r}"))
        if isinstance(root, dict) and "@context" not in root:
            findings.append(Finding(source, index, "$", "-", "ERROR", "missing @context"))

    found_any = False
    for path, node in iter_entities(data):
        found_any = True
        if is_reference(node):
            continue  # References are validated where the entity is defined.
        for t in types_of(node):
            base = t if t in RULES else SUBTYPES.get(t)
            if not base:
                findings.append(Finding(source, index, path, t, "OK", "type not in rule set (not checked)"))
                continue
            issues = check_entity(node, base)
            if not issues:
                findings.append(Finding(source, index, path, t, "OK", "all checked properties present"))
            for level, msg in issues:
                findings.append(Finding(source, index, path, t, level, msg))
    if not found_any:
        findings.append(Finding(source, index, "$", "-", "ERROR", "no entities with @type found"))
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Extract and validate JSON-LD structured data.")
    parser.add_argument("sources", nargs="+", help="URLs, .html files or .json/.jsonld files")
    parser.add_argument("--csv", help="Also write all findings to this CSV file")
    parser.add_argument("--quiet", action="store_true", help="Only print errors and warnings")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    all_findings: list[Finding] = []
    for source in args.sources:
        try:
            content, kind = load_source(source, args.timeout)
        except Exception as exc:  # noqa: BLE001 - report any load failure and move on
            all_findings.append(Finding(source, 0, "-", "-", "ERROR", f"could not load: {exc}"))
            continue
        blocks = [content] if kind == "json" else extract_blocks(content)
        if not blocks:
            all_findings.append(Finding(source, 0, "-", "-", "ERROR", "no JSON-LD blocks found"))
        for i, raw in enumerate(blocks, 1):
            all_findings.extend(validate_block(source, i, raw))

    icons = {"ERROR": "✗", "WARNING": "!", "OK": "✓"}
    current = None
    for f in all_findings:
        if args.quiet and f.level == "OK":
            continue
        if f.source != current:
            current = f.source
            print(f"\n== {f.source}")
        print(f"  {icons[f.level]} [{f.level:<7}] block {f.block} {f.entity_type:<16} {f.path}: {f.message}")

    errors = sum(f.level == "ERROR" for f in all_findings)
    warnings = sum(f.level == "WARNING" for f in all_findings)
    print(f"\nSummary: {errors} error(s), {warnings} warning(s) across {len(args.sources)} source(s).")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["source", "block", "path", "type", "level", "message"])
            for f in all_findings:
                writer.writerow([f.source, f.block, f.path, f.entity_type, f.level, f.message])
        print(f"CSV report written to {args.csv}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
