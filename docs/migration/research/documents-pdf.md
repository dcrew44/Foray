# documents-pdf: Python equivalents for Foray's document stack, measured for parity

Date: 2026-10-04. Machine: macOS arm64, Node v26.0.0, Python 3.14.5 (uv-managed), uv 0.11.17.
Everything here was run against the repo's own functions, called from scratch scripts that import the
repo modules by absolute path. Nothing in the worktree was modified, and `git status` was clean after
every run. Scripts and outputs are in
`/private/tmp/claude-501/-Users-haydenbarolette-Projects-JobSearchAgent--claude-worktrees-middleware-django-migration-389b1a/b278fcc1-e5d8-4717-8b99-8e18b04adab6/scratchpad/research/documents-pdf/`
(referred to as `$R` below).

Legend: **[ran]** = I executed it and the numbers come from that run. **[doc]** = taken from docs or
package metadata and not executed.

## TL;DR

| TS piece | Python pick (version) | Measured parity |
|---|---|---|
| `unpdf` 1.8.1 (pdf.js 6.1.200) `extractText({mergePages:true})` | **pypdfium2 5.14.0** + a 30-line pdf.js-compat normaliser + the unpdf merge regexes | Byte-identical text on 9/19 PDFs, covering all clean real-world-style ones (Chromium, Quartz, ReportLab). It differs only where letter-spacing, small caps or broken fonts are involved. The garble decision matched on 18/19. |
| `garble.ts` | Hand port that uses UTF-16 lengths and an explicit JS `\s` class | Identical metrics, reasons and flag on 11/11 synthetic cases and on every PDF whose text is identical. A naive port fails 2/11. |
| `renderPageAsImage` (scale 2, `@napi-rs/canvas`) | **pypdfium2** `page.render(scale=2)` | Same 1224x1584 size on every page, visually identical, mean pixel diff 0.2 to 4.2 out of 255 (antialiasing only), about 4x faster. |
| `mammoth` 1.12.3 `extractRawText` | **python-mammoth**: pin **1.12.2** to match JS 1.12.3, or move **both** to **1.13.0** | Identical on plain.docx and on a stress docx (tables, footnotes, tracked changes, textbox, symbols, breaks). 1.13.0 differs from JS 1.12.3 on `w:customXml`. |
| `clean.ts` (regex only, no parser) | **A straight port using stdlib `re`** + **ada-url 4.0.0** for `new URL()` | Identical on 159/159 inputs: every string of 80+ characters in the greenhouse, lever, ashby and jsearch fixtures, both careers pages, and 16 edge cases. The one error case raises in both. Links matched 18/18 with ada-url and 10/18 with `urllib.parse.urljoin`. |
| `worker/pdf.ts` (playwright-core 1.63.0) | **playwright (Python) 1.63.0**, async API | Byte-identical PDFs except `/CreationDate` and `/ModDate` on 6/6 documents, both with local Chrome 154 and with bundled headless-shell 153. Same page counts, text and pixels. |

Libraries to avoid: **PyMuPDF** (AGPL-3.0 or a commercial licence) and **html2text** (GPL-3.0-or-later; it was
pulled in for the comparison and is not needed). selectolax, lxml and BeautifulSoup do **not** give
parity with `clean.ts` (details in section 4).

---

## Versions actually installed and tested

Python venv `$R/venv` (`uv venv --python 3.14`, then `uv pip install …`). All were the latest on PyPI on
2026-10-04, checked with the PyPI JSON API:

| package | version | licence (from installed METADATA / PyPI) |
|---|---|---|
| pypdfium2 | 5.14.0 (PDFium 156.0.8076.0; wheel uploaded 2026-10-04T15:18) | Apache-2.0 OR BSD-3-Clause; PDFium BSD-3 plus dependency licences |
| pypdf | 6.19.0 | BSD-3-Clause |
| pdfminer.six | 20260107 | MIT |
| pdfplumber | 0.11.10 | MIT |
| mammoth | 1.13.0 (also tested 1.12.2 in `$R/mammoth-old/venv`) | BSD-2-Clause |
| playwright | 1.63.0 (bundled driver = playwright-core 1.63.0 on Node v24.21.0, 141 MB) | Apache-2.0 |
| ada-url | 4.0.0 (bundles Ada 4.0.0) | Apache-2.0 |
| selectolax | 1.0.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| beautifulsoup4 | 4.15.0 | MIT |
| html2text | 2025.4.15 | **GPL-3.0-or-later** |
| Pillow 12.3.0, numpy 2.5.3, reportlab 5.0.1 | test tooling only | MIT-CMU / BSD / BSD |
| PyMuPDF (not installed) | 1.28.2 on PyPI | **AGPL-3.0 or Artifex commercial** |
| pdftext (not installed) | 0.7.1 on PyPI | Apache-2.0 (built on pypdfium2; reorders blocks, so no pdf.js parity) |

TS side, read from the repo's `node_modules`: unpdf 1.8.1, which bundles pdf.js **6.1.200** (build
6353acefe, read via `getResolvedPDFJS()`); mammoth 1.12.3 (npm latest is 1.13.0, published
2026-09-26); @napi-rs/canvas 1.0.9 (npm latest 1.0.10); playwright-core 1.63.0; docx 9.7.1 (used to
generate a test document). Node's bundled Ada is 3.4.4 with ICU 78.3 and Unicode 17.0. Python's
`unicodedata` is Unicode 16.0.0.

Browsers: local Google Chrome 154.0.8037.98 (the TS worker's dev fallback, `channel: "chrome"`). I also
installed Playwright's `chromium_headless_shell-1243` (153.0.8010.12) into `$R/ms-playwright` with
`PLAYWRIGHT_BROWSERS_PATH`, so nothing went into the global cache. MCR has
`mcr.microsoft.com/playwright/python:v1.63.0-noble` (checked via the registry tag list) to match the
existing `mcr.microsoft.com/playwright:v1.63.0-noble` worker base image.

---

## 1. PDF text extraction (`src/lib/resume/parse/pdf.ts` and `garble.ts`)

### What the TS code actually produces [ran, read]
`parsePdf` calls `unpdf.extractText(bytes, {mergePages:true})`. Reading `node_modules/unpdf/dist/index.mjs`,
that function:
- for each page, takes `getTextContent().items`, keeps those with `str != null`, and joins `item.str + (item.hasEOL ? "\n" : "")`;
- joins the pages with `"\n"`;
- then runs `.replace(/[^\S\n]+/g," ").replace(/ ?\n ?/g,"\n").replace(/\n{3,}/g,"\n\n")`.

pdf.js also NFKC-normalises a fixed set of code points before this (`normalizeUnicode` in the bundled
`pdfjs.mjs`: ligatures U+FB00 to U+FB06, NBSP, U+2000 to U+200A, U+2126, Arabic presentation forms, and
others). I also measured that it turns U+2010 and U+2011 into `-`.

### Method [ran]
`$R/dump_ts.mts` and `$R/parse_any.mts` run the repo's `parsePdf` through
`node_modules/.bin/tsx`. `$R/py_pdf_text.py`, `$R/py_sweep.py` and `$R/py_adversarial.py` extract the
same bytes with each Python candidate, apply the same merge normalisation, and diff the results. The
diffs report exact equality, line-level and word-sequence `difflib` ratios, a bag-of-words
missing/extra count, and garble metrics.

### Repo fixtures, raw library output (no compat layer) [ran]
```
fixture        lib              exact line  wordseq char  missing extra  ms
single-column  pypdf            False 0.898 0.988  0.998     5      5    26.6
single-column  pypdf-layout     False 0.678 0.938  0.989    10     43    47.4
single-column  pdfminer         False 0.475 0.949  0.960     5      5    44.7
single-column  pdfium-range     True  1.000 1.000  1.000     0      0     2.3
single-column  pdfplumber       True  1.000 1.000  1.000     0      0    55.9
two-column     pypdf            False 0.931 0.987  0.997     6      6    44.3
two-column     pdfminer         False 0.389 0.790  0.772     6      6    59.3
two-column     pdfminer-noflow  False 0.389 0.758  0.741     6      6    56.8
two-column     pdfium-range     True  1.000 1.000  1.000     0      0     3.0
two-column     pdfplumber       False 0.542 0.817  0.764     0      0    71.7
serif-dense    pypdf            False 0.830 0.975  0.996     9     20    32.2
serif-dense    pdfminer         False 0.545 0.956  0.965     4      4    53.4
serif-dense    pdfium-range     False 0.830 0.964  0.988    31     12     3.4
serif-dense    pdfplumber       False 0.887 0.925  0.991    78      9    65.9
```
(`pdfium-bounded`, which uses `get_text_bounded()`, gave the same result as `get_text_range()`.)
- **pypdfium2 is the only library that reproduces pdf.js's content-stream order and line breaks.** That
  includes the interleaved two-column order the extraction prompt is written to cope with. pdfminer and
  pdfplumber re-flow into layout order, so on two-column the line ratio drops to about 0.4 to 0.5.
  pypdf's line breaks drift.
- pypdfium2 is also 10 to 20x faster (about 3 ms per 2-page resume, against 26 to 70 ms).

### The compat layer (`$R/pdf_text.py`) [ran]
```python
_PDFJS_NFKC = re.compile("([ µ;ຳ -  Ωﬀ-ﬄﬆ…ﹽ]+)|(ﬅ+)")  # copied from pdf.js 6.1.200
_HYPHENS = str.maketrans({"‐": "-", "‑": "-"})
def extract_pages(data: bytes) -> list[str]:
    doc = pdfium.PdfDocument(data)
    try:
        out = []
        for page in doc:
            tp = page.get_textpage(); text = tp.get_text_range(); tp.close(); page.close()
            text = _PDFJS_NFKC.sub(lambda m: unicodedata.normalize("NFKC", m.group(1)) if m.group(1) else "ſt"*len(m.group(2)), text)
            out.append(text.translate(_HYPHENS).rstrip())   # pdf.js pages end without trailing ws
        return out
    finally:
        doc.close()
def merge_pages(texts):  # unpdf normalizeMergedText, with JS \s spelled out
    s = "\n".join(texts); s = _NON_NL_WS.sub(" ", s); s = re.sub(r" ?\n ?", "\n", s); return re.sub(r"\n{3,}", "\n\n", s)
```

### Sweep over 19 PDFs from four producers (`$R/py_sweep.py`) [ran]
The corpus is the 3 repo fixtures, the same 3 re-rendered, the template at normal and compact density,
a letter-spacing ladder, 7 adversarial PDFs, a glyph test, a macOS Quartz PDF (`cupsfilter`) and a
ReportLab two-frame PDF using standard-14 fonts.
```
pdf                              raw exact compat exact compat wordseq garbled ts/py
resumes/serif-dense.pdf          False     False        0.969          F/F
resumes/single-column.pdf        True      True         1.000          F/F
resumes/two-column.pdf           True      True         1.000          F/F
ts-bundled/letterspacing.pdf     False     False        0.870          T/T
ts-bundled/template-compact.pdf  True      True         1.000          F/F
ts-bundled/template-normal.pdf   True      True         1.000          F/F
adversaria/glued.pdf             False     False        0.984          T/T
adversaria/glyphs.pdf            False     False        0.906          F/F
adversaria/no-tounicode-*.pdf    False     False        0.07-0.16      T/T   (both garbage, both flagged)
adversaria/scan.pdf              True      True         1.000          T/T
adversaria/track-0.12.pdf        False     False        0.073          T/F DIFF
adversaria/track-0.15.pdf        False     False        0.871          T/T
adversaria/track-0.20.pdf        False     False        1.000          T/T
producers/quartz-txt.pdf         False     True         1.000          F/F
producers/reportlab-2col.pdf     True      True         1.000          F/F
exact raw: 8/19   exact compat: 9/19   garble decision equal: 18/19   metrics bit-identical: 10/19
```
(The re-rendered copies of single-column, two-column and serif-dense gave the same results as the
originals. Those rows are trimmed above.) On ReportLab output, pypdf, pdfminer and pdfplumber turned the
bullet glyph into `\x7f` or `(cid:127)`. pypdfium2 and pdf.js both gave `•`.

### Remaining text gaps, all measured [ran]
1. **Letter-spacing.** This is the important one, because `garble.ts` reasons about it. The ladder
   test (`$R/render/html/letterspacing.html`, rows from 0.00em to 0.30em, sans and serif) showed:
   pdf.js splits every glyph from **0.11em** upward, which matches its 0.102 x font-size tracking
   threshold. PDFium keeps words whole up to 0.13em, splits them partially from 0.14 to 0.17em
   (`E X PERIE N CE S umma r y`), and splits fully from 0.18em. Consequences:
   - `serif-dense` name line: pdf.js gives `I L O N A B E R G Q U I S T- M A H O N E Y`, PDFium gives `ILONA BERGQUI ST-MAHONEY`. Its single-character-word ratio is 0.232 against 0.202; both are below 0.4, so the decision is the same.
   - `track-0.12.pdf` (template body at 0.12em): pdf.js gives 100% single-character words, so it is **garbled** and goes to the vision path. PDFium gives clean text, ratio 0.18, **not garbled**, so it goes to the text path. This is the only decision flip in the sweep. PDFium's text here is better, but it is still a parity break.
   - Bands outside 0.11 to 0.17em behave identically.
2. **Small caps** (`font-variant: small-caps`). pdf.js gives `LANGUAGES` and `SMALL CAPS LABEL`. PDFium gives the source case (`Languages`, `Small Caps Label`). The PDFs have no `/ActualText`. `text-transform: uppercase` gives uppercase in both.
3. **Glued text** (`word-spacing:-0.28em`). Both flagged garbled. PDFium dropped one more space (`DanaReyes`).
4. **No /ToUnicode** (fonts stripped with pypdf). Both produce garbage and both flag it, for slightly different reasons: pdf.js also trips "word spacing lost" because it emits glyph-id `\x03` where a space would be.
5. Hyphen variants and trailing page whitespace are fixed by the compat layer. Ligatures came out the same from Chromium PDFs in both.

### Garble port (`$R/garble.py`) [ran]
Parity traps that a naive port gets wrong:
- JS `.length` counts UTF-16 code units. `js_len(s) = len(s) + count(ord(c) > 0xFFFF)`.
- JS `\s` is not Python `\s`. Python's `str.isspace()` class adds U+001C to U+001F and U+0085 and drops U+FEFF. Use an explicit class: `"\t\n\v\f\r    -     　﻿"`.
- `\p{L}` is not supported by `re`. `str.isalpha()` covers exactly the L* categories (Unicode 16 in Python against 17 in Node).
- In the reason strings, `Math.round` rounds half up and `toFixed` breaks exact ties upward. Implemented with `floor(x+0.5)` and `Decimal(x).quantize(ROUND_HALF_UP)`.

Results (`$R/garble_parity.py`): port identical on **11/11** cases. The naive port's metrics were
identical on only 9/11: the astral-character case gave alphaRatio 0.54 against 0.95, and the U+FEFF case
gave spaceRatio 0.214 against 0.190. The DOCX garble report was also identical. Watch the JSON shape:
the upload route returns `garble` to the client, so keep the camelCase keys. Python serialises
`2988.0` where JS writes `2988`; that is harmless to a JS reader.

### Error behaviour (`$R/broken/`) [ran]
Both libraries accept and reject the same files. Bad xref and owner-password-only files opened in both.
Truncated files, garbage, and a .docx renamed to .pdf were rejected by both. A user-password PDF was
rejected by both. The **messages differ**, and the upload route echoes them to the user (`Could not read
that resume: ${message}`): pdf.js says `Invalid PDF structure.` and `No password given`, while
pypdfium2 says `PdfiumError: Failed to load document (PDFium: Data format error).` and `(… Incorrect
password error)`. Map the PdfiumError codes to the existing wording.

### Operational notes
- **PDFium is not thread-safe.** [doc: pypdfium2 METADATA "Known limitations: Incompatibility with Threading"] Extraction currently runs **inline in the upload request** (`src/app/api/uploads/route.ts`), so in Django wrap every pypdfium2 call in one process-wide `threading.Lock`, or run it in a process pool. The calls take about 3 ms to extract and about 15 ms per page to render, so a lock costs little.
- The transition risk is low. The only consumer is the upload route. `documents.text_extracted` is stored, but nothing hashes or dedupes on it (`sha256` is taken over the file bytes). The extractor can switch wholesale when that route moves.

## 2. PDF page to image for the vision fallback [ran] (`$R/py_render.py`)
```python
bitmap = page.render(scale=2)            # white fill by default
bitmap.to_pil().save(buf, format="PNG")  # RGB
```
| fixture/page | TS (pdf.js + @napi-rs/canvas) | pypdfium2 | mean abs diff (0-255) | pixels with diff > 32 |
|---|---|---|---|---|
| single-column p1/p2 | 1224x1584 RGBA, 364/246 KB | 1224x1584 RGB, 299/207 KB | 2.63 / 1.90 | 2.96% / 2.15% |
| two-column p1/p2 | 490/107 KB | 405/86 KB | 2.79 / 0.58 | 3.15% / 0.62% |
| serif-dense p1/p2 | 518/41 KB | 442/36 KB | 4.17 / 0.20 | 4.88% / 0.25% |

The TS alpha channel is fully opaque (extrema 255,255), so RGB loses nothing. A side-by-side crop
(`$R/render-compare-serif.png`) is visually identical: the differences are glyph antialiasing (ink IoU
about 0.77). Speed: pypdfium2 about 30 ms for 2 pages. TS took 823 ms on its first call (including the
canvas import) and 120 to 130 ms after that. PNGs are about 15% smaller, and dimensions, which drive
vision token cost, are identical. The same thread lock applies. `MAX_VISION_PAGES = 4` ports directly.

## 3. DOCX (mammoth) [ran] (`$R/py_docx.py`, `$R/docx_stress.mjs`)
- `plain.docx`: `mammoth.extract_raw_text` 1.13.0 gave text identical to JS 1.12.3 (2988 characters). The garble report was identical.
- I generated a stress docx with the repo's own `docx` 9.7.1: tabs, `w:br`, `w:sym`, no-break and soft hyphens, emoji and CJK, a footnote, a comment, tracked insert and delete, a hyperlink, nested bullets, a two-row table with a multi-paragraph cell, page and column breaks, allCaps and smallCaps runs, a hidden run, a checkbox, a textbox, and header and footer. Raw text was **identical** between JS 1.12.3 and Python 1.13.0, including the warning `A w:sym element with an unsupported character was ignored: char 2612 in font MS Gothic`. `convert_to_html` (not used by the app) differed only in checkbox attribute order.
- **Version gap found:** I wrapped a run in `<w:customXml>`. JS 1.12.3 and Python 1.12.2 both **drop** it. Python 1.13.0 and JS 1.13.0 both **keep** it (`… CUSTOMXML-RUN`); I installed JS 1.13.0 into `$R/npm-mammoth13` and Python 1.13.0 matched it on all three documents. Changelogs [doc]: python-mammoth 1.13.0 "Read children of w:customXml elements; Handle paragraphs and runs tracked as revisions". mammoth.js 1.13.0 has the same changes. The mammoth.js 1.12.2 prototype-pollution fix is JS-only. Recommendation: bump JS to 1.13.0 now (`pnpm up mammoth`) and use python-mammoth 1.13.0. Otherwise pin python-mammoth==1.12.2 until the TS side is retired.
- `mammoth.extract_raw_text` takes a file-like object, for example `io.BytesIO(data)`. It is pure Python, so it is thread-safe and needs no lock.

## 4. HTML to text and cleaning (`src/lib/sources/clean.ts` and `careers-page.ts`) [ran]
**What it relies on:** no HTML library at all. It is regex replacements, a 15-entry entity table,
`String.fromCodePoint`, `Number.parseInt`, `.length` and `.slice` in UTF-16 units, and WHATWG
`new URL(href, base)`. `careers-page.ts` adds `.slice(0, MAX_LISTING_CHARS)` and `MAX_POSTING_CHARS`,
then `resolveUrl()` via `new URL`, then `webJobId(slug, url) = sha1(url)` and `contentHash(JSON.stringify([...]))`.

**Python equivalent: a literal port with `re`** (`$R/clean.py`). Parity traps handled:
- Every `\s` becomes the explicit JS whitespace class. Patterns that use `\w` and `\b` are compiled with `re.ASCII`, because JS `\w` and `\b` are ASCII-only. `re.ASCII|re.IGNORECASE` also reproduces JS's non-`u` case folding (no `ſ`→`s`, no KELVIN SIGN→`k`).
- `decodeEntities`: `Number.parseInt("123abc",10)` is 123, and `&#e5;` is NaN so the entity is left as written. The port takes the leading-digit prefix. `String.fromCodePoint(>0x10FFFF)` throws `RangeError` in TS, and the port raises `ValueError`; the edge case `&#99999999;` throws in both.
- The `cleanDescription` tail cutoff (`text.length`, `paragraph.length + 2`) is computed in UTF-16 units.
- JS `.trim()` uses the JS whitespace class, and `$` must be written `\Z`.

Results (`$R/dump_clean.mts` vs `$R/py_clean_compare.py`), corpus of **159** inputs: every string of
80+ characters in the 7 ATS fixture JSONs, both careers pages with their base URLs, and 16 edge cases
(parseInt prefix, NBSP and U+FEFF/U+0085/U+001C, Kelvin and long s, astral characters at the cutoff,
uppercase SCRIPT/STYLE, entity-escaped markup, CRLF, comments, CDATA, `>` inside attributes, lone
surrogate, NUL, huge entity):
```
htmlToText {'equal': 158, 'both-error': 1}
withLinks  {'equal': 158, 'both-error': 1}
clean      {'equal': 158, 'both-error': 1}
```
Speed on the 400 KB retool page, averaged over 20 runs: TS 0.6, 0.8 and 0.8 ms; Python 2.9, 4.1 and
4.8 ms.

**URL resolution: use `ada_url.join_url(base, href)`, not `urllib.parse.urljoin`.** Node's `URL` is
built on Ada (Node 26 ships Ada 3.4.4). Against TS `htmlToTextWithLinks` on 18 cases, ada-url matched
**18/18** and urljoin matched **10/18**. urljoin fails to percent-encode spaces and non-ASCII
characters, keeps `:443`, does not lowercase the host or add the root `/`, does no IDNA (`bücher` vs
`xn--bcher-kva`), does not treat `\` as `/`, and does not parse the IPv4 shorthand `0x7f.1`. This
matters beyond display: `webJobId = sha1(url)`, so a different URL string means a different job id. In
a shared database during the strangler period, that would create duplicate `web:` jobs.

**Hashing and slicing next to cleaning** (`$R` inline test, matched against Node):
- `text.slice(0, 20000)` must slice UTF-16 units: `s.encode("utf-16-le","surrogatepass")[:2*n].decode("utf-16-le","surrogatepass")`. It can leave a lone high surrogate, exactly as JS does.
- `createHash().update(str)` encodes lone surrogates as U+FFFD. `JSON.stringify` escapes them as `\udXXX`. `json.dumps` must use `ensure_ascii=False, separators=(",",":")` plus that escape. With all three in place, the Python sha1 equalled the Node sha1 (`4a468870…`). The naive version, and a version with `salaryMin` as the float `120000.0`, both produced different hashes. psycopg also **refuses** lone surrogates (`UnicodeEncodeError: surrogates not allowed`), whereas node-pg writes U+FFFD. Sanitise before storing.
- `[...locations].sort()` compares UTF-16 code units, and Python `sorted` compares code points. They only differ for astral against U+E000 to U+FFFF characters.

**DOM libraries (selectolax/lexbor, BeautifulSoup+lxml): measured drift, not parity** (`$R/py_dom_alt.py`).
Even with matching block and `<li>` rules, line-sequence similarity to TS was 0.963 to 0.989, and 0.881
on an ashby description, which had 183 lines against 144. They decode all HTML5 entities, handle
comments, CDATA and attributes properly, and differ on whitespace. That may be better text, but it is
not the same text, and `description_text` feeds `contentHash` on careers pages. Adopt one only as a
deliberate change after the TS ingest is retired, and regenerate `description_text` when you do (it is
disposable by design). selectolax 1.0.0 is MIT and fast (about 10 ms for 400 KB). html2text is GPL; do
not use it.

## 5. Resume PDF rendering (`src/worker/pdf.ts`) [ran]
`$R/ts_render.mts` imports the **real** `renderPdf` (`SKIP_ENV_VALIDATION=1`). `$R/py_render_pdf.py` is
the Python port:
```python
browser = await p.chromium.launch(headless=True, handle_sigint=False, handle_sigterm=False, handle_sighup=False)
context = await browser.new_context(java_script_enabled=False)
await context.route("**/*", lambda route: route.abort())
page = await context.new_page(); page.set_default_timeout(30_000)
await page.set_content(html, wait_until="load")
pdf = await page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)
```
Rendered documents: `renderResumeHtml(resume)` at normal and compact density, the 3 fixture HTMLs, and
the letter-spacing ladder. The exact HTML strings the TS run produced were saved and fed to Python.
`$R/py_render_compare.py`:
```
chrome   template-normal   bytes_equal=False equal_ignoring_dates=True text_equal=True max_pixel_diff=0 mediabox=(0,0,612,792) pages=2
… (all 6 docs × {Chrome 154 via channel="chrome", bundled headless shell 153}: equal_ignoring_dates=True, max_pixel_diff=0)
chrome154 vs shell153: equal_ignoring_dates=False, text_equal=True   (browser build matters, language doesn't)
```
Byte sizes were identical between TS and Python for every document in both browser modes. The only
byte differences are `/CreationDate` and `/ModDate`, which also make two TS renders differ, so the
existing sha256 dedupe in `renderStoredResume` behaves the same. The reason is that Python Playwright
1.63.0 drives the **same** playwright-core 1.63.0 JS driver, on its bundled Node v24.21.0. The
`countPdfPages` regex ports as-is (latin-1 decode) and agreed with pypdfium2's page count.

Constraints for Django [ran]:
- The sync API raises `Error: It looks like you are using Playwright Sync API inside the asyncio loop`. Use the async API in an async worker, or the sync API on a dedicated thread.
- Playwright objects are bound to the thread or loop that created them. A browser created in another thread failed with `cannot switch to a different thread`. The TS "one shared browser, a context per render" pattern ports cleanly as one browser per worker event loop or process. It does not port to a browser shared across Django request threads.
- The worker image can use `mcr.microsoft.com/playwright/python:v1.63.0-noble` (tag verified). Or install `playwright install --with-deps chromium` on top of a uv-managed Python 3.14 image. Which Python version the MCR image ships was not checked; see open questions.
- PDF fonts come from the OS (`"Helvetica Neue", Helvetica, Arial, "Liberation Sans"`). Parity across the TS and Python workers holds only if both use the same image and browser build. That is already true of TS dev (Chrome 154) against prod (shell 153), which produce different bytes and the same text.

## Recommendation
- Use **pypdfium2 5.14.0** for both text extraction and vision rendering, behind a module-level lock.
  Add the pdf.js-compat normaliser and the unpdf merge regexes, and port `garble.ts` with UTF-16 and JS
  whitespace semantics. Freeze the TS outputs for every fixture as golden files now, before the TS side
  is deleted. The Python test then asserts byte equality on single-column and two-column, and a
  documented allowlist for serif-dense: letter-spaced name, small-caps labels.
- Accept the 0.11 to 0.13em letter-spacing decision flip and document it (PDFium yields usable text
  there), or re-tune `MAX_SINGLE_CHAR_WORD_RATIO` against a larger real-upload corpus.
- Bump JS mammoth to 1.13.0 and use **python-mammoth 1.13.0**.
- Port `clean.ts` literally with `re` plus **ada-url 4.0.0**, and use the UTF-16, surrogate and JSON helpers
  for slicing and hashing.
- Use **playwright 1.63.0 (async)** for rendering, in the matching `playwright/python:v1.63.0` image.

## Parity gaps (summary)
1. Letter-spacing between 0.11 and 0.17em is tokenised differently by PDFium and pdf.js. In the
   0.11 to 0.13em band, a uniformly tracked document flips from vision to text.
2. Small-caps text comes out in source case in PDFium and uppercase in pdf.js.
3. Error messages for invalid or encrypted PDFs differ; map them.
4. On glued text, PDFium drops a space or two more.
5. python-mammoth 1.13.0 is not the same as JS mammoth 1.12.3 on `w:customXml`. Bump or pin.
6. `urllib.parse.urljoin` is not WHATWG. Without ada-url, `webJobId` changes and creates duplicate jobs.
7. Hashing: Python must emulate UTF-16 slicing, `JSON.stringify` (separators, no ASCII-escaping, `\ud…` for lone surrogates), int and float formatting, and U+FFFD encoding. psycopg rejects lone surrogates.
8. Rendered PDFs are byte-identical apart from timestamps for the same browser build. A different
   Chromium build changes bytes but not text.
9. Vision PNGs are RGB rather than RGBA, about 15% smaller, with only antialiasing-level pixel
   differences.

## Sources
- Executed code: every script under `$R` (listed above). TS functions were imported from the worktree's `src/lib/resume/parse/{pdf,docx,garble}.ts`, `src/lib/sources/clean.ts`, `src/worker/pdf.ts` and `src/lib/resume/render/template.ts`.
- unpdf internals: `node_modules/unpdf/dist/index.mjs` (`extractText$1`, `normalizeMergedText`); pdf.js `normalizeUnicode` in `node_modules/unpdf/dist/pdfjs.mjs`.
- pypdfium2 thread-safety caveat: installed `pypdfium2-5.14.0.dist-info/METADATA`, "Known limitations / Incompatibility with Threading" (links to https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading).
- mammoth changelogs: https://raw.githubusercontent.com/mwilliamson/python-mammoth/master/NEWS and https://raw.githubusercontent.com/mwilliamson/mammoth.js/master/NEWS (fetched 2026-10-04).
- PyPI JSON API (https://pypi.org/pypi/<pkg>/json) for latest versions and licences (pymupdf: "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial"); `npm view` for mammoth, unpdf and @napi-rs/canvas.
- MCR tag lists: https://mcr.microsoft.com/v2/playwright/python/tags/list and https://mcr.microsoft.com/v2/playwright/tags/list.
- Node's URL implementation version: `process.versions.ada` = 3.4.4. ada-url bundles `ADA_VERSION "4.0.0"` (`ada_url/ada.h`).

---

## Verification (adversarial pass, 2026-10-04)

Verifier venv: `$V = …/scratchpad/research/documents-pdf-verify/venv` (`uv venv --python 3.14` → CPython 3.14.5,
GIL enabled; `uv pip install pypdfium2 pypdf pdfminer.six mammoth playwright ada-url selectolax html2text pymupdf pdfplumber`).
Resolved: pypdfium2 5.14.0 (PDFium 156.0.8076.0, origin pdfium-binaries), pypdf 6.19.0, pdfminer.six 20260107,
mammoth 1.13.0, playwright 1.63.0, ada-url 4.0.0, selectolax 1.0.0, html2text 2025.4.15, pymupdf 1.28.2,
pdfplumber 0.11.10. Each is the current PyPI release (PyPI JSON API, `$V/../pypi_licenses.py`). Scripts are in
`…/scratchpad/research/documents-pdf-verify/`.

### Claim 1: "PDFium is not thread-safe" — CONFIRMED by execution [ran] (it was [doc] before)

Source reading [read]:
- pypdfium2 5.14.0 has no lock anywhere (`grep -rn 'thread|Lock(' pypdfium2 pypdfium2_raw` finds nothing relevant).
  `pypdfium2_raw/bindings.py` loads `libpdfium.dylib` with `dllclass = ctypes.CDLL`. CDLL releases the GIL for
  every foreign call, so the GIL does **not** serialise PDFium calls.
- `PdfDocument(bytes)` goes straight to `FPDF_LoadMemDocument64` (`_helpers/document.py::_open_pdf`), with no
  Python callback in the path. So any failure under concurrency happens inside PDFium.
- Upstream header `public/fpdfview.h` (pdfium main): "None of the PDFium APIs are thread-safe. They expect to be
  called from a single thread." pypdfium2 docs (stable, 5.14.0) say you may not call PDFium at the same time from
  different threads, "not even with different documents". Doing so can "crash or corrupt the process".

Experiment [ran] (`thread_stress.py <mode> <threads> <iters>`): 11 PDFs (repo fixtures as re-rendered, the
template, ReportLab, Quartz and adversarial PDFs; the 0-byte `producers/quartz-rtf.pdf` is skipped). Each task
opens its **own** `PdfDocument` from bytes. That is the realistic Django case, where each request parses its
own upload. Each task hashes `get_text_range()` for every page plus a `render(scale=1.5)` of page 0, and the
hash is compared with a serial baseline. Each run is a separate process, so crashes show up as exit codes.
```
mode=serial threads=1 iters=200 wall=1.25s mismatches=0 errors=0                       exit=0
mode=lock   threads=8 iters=200 wall=1.24s mismatches=0 errors=0                       exit=0
nolock threads=2  4 runs: exit 133 (SIGTRAP), 139 (SIGSEGV), 0 [74 wrong, 72 PdfiumError], 0 [62 wrong, 58 errors]
nolock threads=4  4 runs: exit 133, 139, 134 (SIGABRT), 133
nolock threads=8  4 runs: exit 133, 0 [173 wrong / 169 errors], 134, 133
TEXT_ONLY=1 (no render, the upload-route case), nolock:
  threads=2: exit 133, 0 [150 wrong / 148 errors], 139, 139
  threads=8: exit 0 [196/190], 139, 0 [197/191], 133
TEXT_ONLY=1 lock threads=8: mismatches=0 errors=0
ProcessPoolExecutor(8), no lock, 3 runs: mismatches=0, exit 0 (0.37-0.50 s for 200 tasks)
```
Errors seen: `Failed to load document (PDFium: Data format error)` on valid files, `Failed to load page.`, and
`Failed to load document (PDFium: Success)`. The last one shows that `FPDF_GetLastError` is process-global and
another thread overwrote it. "wrong" > "errors" means some calls returned **silently wrong** text or pixels with
no exception. Even 2 threads doing text extraction alone crash the interpreter in most runs.

Verdict: **not refuted.** Refinements for the plan:
- The lock must be **one process-wide lock around every PDFium call, including `close()`**, not one lock per
  document. pypdfium2 also attaches `weakref.finalize` closers (`internal/bases.py`). An object that is not closed
  explicitly can be closed by GC on any thread, outside the lock. So always close explicitly inside the locked
  block, as the researcher's `extract_pages` does with `try/finally`.
- This applies to any Django threaded server (gunicorn `gthread`, a threaded runserver, ASGI `sync_to_async`
  thread pools) and to a threaded worker. A process pool needs no lock (measured above).
- Any other library in the same process that loads PDFium (pdfplumber does not; pdftext does, through
  pypdfium2) must share the same lock.

### Claim 2: licences — CONFIRMED [ran: installed METADATA, bundled licence files, PyPI JSON]

| package (current) | METADATA `License-Expression` / `License` | what the shipped files show |
|---|---|---|
| pymupdf 1.28.2 | `License: Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License` (same on PyPI) | ships `libmupdf.dylib`; MuPDF headers (`fitz.h`, `pdf.h`) carry AGPL notices. **AGPL or commercial, confirmed.** |
| html2text 2025.4.15 | `GPL-3.0-or-later` | `licenses/COPYING` = GNU GPL v3. **Confirmed.** |
| pypdfium2 5.14.0 | `BSD-3-Clause, Apache-2.0, dependency licenses` | 26 source files have SPDX `Apache-2.0 OR BSD-3-Clause`; docs and README are CC-BY-4.0. Bundled `BUILD_LICENSES`: pdfium BSD-3, pdfium-binaries MIT, FreeType FTL, ICU Unicode-3.0, lcms2 MIT, libjpeg-turbo IJG+BSD, openjpeg BSD-2, libpng, zlib, abseil Apache-2.0, llvm-libc Apache-2.0 WITH LLVM-exception, AGG 2.3, fast_float MIT, simdutf MIT. All permissive. These licences must be shipped with the binary. |
| pypdf 6.19.0 | `BSD-3-Clause` | permissive |
| pdfminer.six 20260107 | `MIT` | permissive (deps charset-normalizer MIT, cryptography Apache-2.0 OR BSD-3) |
| mammoth 1.13.0 | `BSD-2-Clause` | permissive (dep cobble 0.1.4: BSD) |
| playwright 1.63.0 | `Apache-2.0` | The wheel bundles `driver/node` (Node v24.21.0, MIT plus permissive deps; the only GPL text in its LICENSE covers ICU's autoconf build scripts, which carry the Autoconf exception and are not in the binary) and playwright-core (Apache-2.0, ThirdPartyNotices has no GPL). The Chromium browser that `playwright install` downloads is **not** in the wheel and was not audited here. |
| ada-url 4.0.0 | `Apache-2.0` | **mismatch:** the bundled `licenses/LICENSE` is the **MIT** text. Ada upstream is dual Apache-2.0/MIT. Permissive either way. |
| selectolax 1.0.0 | `MIT` | Only the Lexbor backend (Apache-2.0) is linked in. `parser.py` now just raises "Modest backend is deprecated since selectolax 1.0", and the `.so` has 0 `modest`/`myhtml` strings. **Caveat:** the last 0.x release (0.4.13 sdist) bundles `modest/LICENSE` = **LGPL-2.1** (553 Modest files). So "permissive" holds only for `selectolax>=1.0`; pin it. Lexbor's own Apache-2.0 notice is not shipped in the wheel's dist-info. |

Verdict: **not refuted.** Every licence named in the claim matches the installed artifacts and PyPI. The nuances
above (ada-url metadata vs LICENSE file, selectolax <1.0 being LGPL, the Chromium download outside the wheel)
don't change any of the claim's labels.
