# Schema Testing

Ready-to-adapt JSON-LD templates for the most common Schema.org types, plus a Python validator that extracts structured data from any URL or file, checks that it parses, and flags missing required and recommended properties.

## What it does

**Templates** (`templates/`): clean, production-style JSON-LD examples using stable `@id`s, absolute URLs, ISO dates and ISO currencies, with GCC examples (AED, SAR, Arabic names, `en-AE` / `ar-AE`):

| File | Type | Typical page |
|---|---|---|
| `organization.json` | Organization | Homepage / About |
| `local-business.json` | LocalBusiness | Branch or location page |
| `product.json` | Product + Offer | Product detail page |
| `faq-page.json` | FAQPage | Any page with a visible FAQ |
| `breadcrumb-list.json` | BreadcrumbList | All deep pages (Arabic example) |
| `article.json` | Article | Blog posts and guides |
| `website-searchaction.json` | WebSite + SearchAction | Homepage |

**Validator** (`validate_schema.py`):

- Accepts a **URL**, a local **HTML** file or a raw **.json / .jsonld** file (several at once)
- Extracts every `<script type="application/ld+json">` block
- Reports **JSON syntax errors** with line and column
- Walks `@graph` arrays and nested entities (for example the `Offer` inside a `Product`)
- Flags missing **required** properties (errors) and **recommended** properties (warnings) per type, with common subtypes such as `BlogPosting` → `Article` and `Dentist` → `LocalBusiness`
- Catches common value mistakes: non-ISO dates, currencies like `"Dirham"` instead of `AED`, prices with symbols (`"AED 1,299"`), relative URLs, breadcrumb positions out of order, and a SearchAction without `{search_term_string}`
- Exits with code `1` when errors are found, so it can run in CI

## Why it matters

Structured data helps search engines and AI systems understand **what** a page is about and **who** is behind it. It also makes pages eligible for rich results such as product prices, breadcrumbs and article details. But:

- One misplaced comma makes the whole block invalid, and it fails silently.
- CMS plugins and developers often ship markup with missing fields, wrong currencies or relative URLs.
- Google's Rich Results Test checks one URL at a time. This script can check many pages or files in one run and fits into a deployment pipeline.

Use this validator for fast bulk checks, and Google's Rich Results Test plus the [Schema.org validator](https://validator.schema.org/) for final confirmation.

## Folder structure

```
schema-testing/
├── README.md
├── LICENSE
├── requirements.txt
├── validate_schema.py            # Extract + validate JSON-LD
├── examples/
│   └── sample-page.html          # Page with deliberate mistakes, for trying the validator
└── templates/
    ├── organization.json
    ├── local-business.json
    ├── product.json
    ├── faq-page.json
    ├── breadcrumb-list.json
    ├── article.json
    └── website-searchaction.json
```

## How to use it

Requires Python 3.10+.

```bash
git clone https://github.com/haninbigleap-max/schema-testing.git
cd schema-testing
pip install -r requirements.txt
```

Validate a live page:

```bash
python validate_schema.py https://example.com/
```

Validate local files (HTML or JSON), including several at once:

```bash
python validate_schema.py examples/sample-page.html
python validate_schema.py templates/*.json --quiet
```

Save the findings as CSV:

```bash
python validate_schema.py https://example.com/ https://example.com/en-ae/blog/ --csv schema_report.csv
```

Options: `--quiet` hides passing checks, `--csv FILE` writes all findings, `--timeout 20` sets the URL fetch timeout.

**Using the templates**

1. Copy the template that matches your page type.
2. Replace every `example.com` value with real data that is **visible on the page**.
3. Keep `@id` values stable and consistent across the site. For example, the organization is always `https://example.com/#organization`.
4. Delete properties you can't fill truthfully. Never add invented ratings or reviews.
5. Validate with `validate_schema.py`, then the Rich Results Test, before deploying.

**Adding your own rules**

Edit the `RULES` dictionary at the top of `validate_schema.py`. Each type has `required`, `recommended` and optional `one_of` lists. Map extra subtypes in `SUBTYPES`.

## Example output

```
$ python validate_schema.py examples/sample-page.html

== examples/sample-page.html
  ! [WARNING] block 1 Product          $: missing recommended property 'image'
  ! [WARNING] block 1 Product          $: missing recommended property 'brand'
  ✗ [ERROR  ] block 1 Offer            $.offers: priceCurrency should be an ISO 4217 code (e.g. AED, SAR): 'Dirham'
  ✗ [ERROR  ] block 1 Offer            $.offers: price should be a plain number without symbols: 'AED 1,299'
  ✗ [ERROR  ] block 2 BreadcrumbList   $: ListItem positions should be 1..n in order, got [1, 3]
  ✓ [OK     ] block 2 ListItem         $.itemListElement[0]: all checked properties present
  ! [WARNING] block 2 ListItem         $.itemListElement[1]: 'item' should be an absolute URL: '/en-ae/air-purifiers/'
  ✗ [ERROR  ] block 3 -                $: JSON syntax error at line 5, column 3: Expecting property name enclosed in double quotes

Summary: 4 error(s), 8 warning(s) across 1 source(s).
```

(Output trimmed. The full run also lists the other missing recommended properties.)

All bundled templates pass:

```
$ python validate_schema.py templates/*.json --quiet

Summary: 0 error(s), 0 warning(s) across 7 source(s).
```

## Limitations

- The rules are a practical subset of Schema.org and Google's documentation, not a full schema validator. Unknown types are listed but not checked.
- Pages that inject JSON-LD with JavaScript need to be rendered first. Save the rendered HTML from your browser or a headless crawler, then validate the file.
- Passing this validator doesn't guarantee rich results. Google decides eligibility per page and per feature.

## License

MIT
