# Ebook Scanner

Scans the entire system for ebooks, extracts metadata, detects exact and content-based duplicates, and categorizes every book into a SQLite database.

## Requirements

### `ebook_scanner.py`

- Python 3.10+
- [Calibre](https://calibre-ebook.com/) (`ebook-meta` must be on `$PATH`)
- `plocate` / `locate` (for fast system-wide file discovery)

```bash
dnf install calibre mlocate
```

### `content_dedup.py`

- [Poppler](https://poppler.freedesktop.org/) — `pdftotext`, `pdfinfo`, `pdftoppm`
- `djvulibre` — `djvutxt`, `djvused`, `ddjvu`
- [Calibre](https://calibre-ebook.com/) — `ebook-convert` (for MOBI, AZW3, CHM)
- [Tesseract](https://github.com/tesseract-ocr/tesseract) 5.x — OCR for image-based ebooks

```bash
dnf install poppler-utils djvulibre calibre tesseract
# Install language packs as needed:
dnf install tesseract-langpack-chi_sim tesseract-langpack-chi_tra \
            tesseract-langpack-deu tesseract-langpack-fra \
            tesseract-langpack-jpn tesseract-langpack-kor \
            tesseract-langpack-rus tesseract-langpack-spa
```

## Usage

```bash
# Step 1: scan & index all ebooks
python3 ebook_scanner.py

# Step 2: find content-based duplicates
python3 content_dedup.py
```

Both scripts are **resumable** — files already processed are skipped on re-runs.

## Output files

| File | Description |
|---|---|
| `books.db` | SQLite database of all indexed ebooks |
| `duplicates.txt` | Byte-identical duplicates grouped by SHA-256 hash |
| `fuzzy_duplicates.txt` | Content-identical duplicates (different bytes, same text) |
| `report.txt` | Summary statistics (format, category, top authors) |

## Supported formats

`.pdf` · `.epub` · `.djvu` · `.mobi` · `.chm` · `.azw3`

## How it works

### `ebook_scanner.py`

1. **Discovery** — runs `locate --regex` to find all ebook files system-wide, filtering out Python venvs, Wine prefixes, Conda environments, and other system noise.
2. **Hashing** — computes a SHA-256 hash of each file's raw bytes.
3. **Metadata** — calls `calibre`'s `ebook-meta` on every file to extract title, author, publisher, language, and tags. Falls back to parsing the filename (handles the common Z-Library `Title (Author) (Z-Library).ext` pattern).
4. **Categorization** — assigns each book to one of 15 categories based on keyword matching against the title, filename, and tags.
5. **Exact deduplication** — any two files with the same SHA-256 hash *and* the same format are considered byte-identical duplicates and written to `duplicates.txt`.
6. **Storage** — all records are upserted into `books.db` (SQLite with WAL mode).

Processing is parallelized across 20 workers; ~7,700 files take roughly 7 minutes.

### `content_dedup.py`

1. **Text extraction** — reads the first ~3,000 characters from each book using the best available tool per format: `pdftotext` (PDF), `djvutxt` (DjVu), EPUB spine via `zipfile`, `ebook-convert` (MOBI, AZW3, CHM).
2. **OCR fallback** — for image-based PDFs and DjVu files where text extraction yields nothing, pages are rendered to images with `pdftoppm`/`ddjvu` and OCR'd with Tesseract. Language is auto-detected from the database field, CJK characters in the file path, or path keywords (`/chinese/`, `/japanese/`, etc.).
3. **Fingerprinting** — normalizes extracted text and hashes it with SHA-256. Files sharing a hash are content-identical regardless of encoding or byte differences.
4. **ISBN matching** — extracts and checksum-validates ISBNs found near an "ISBN" label; pairs sharing an ISBN are confirmed as duplicates via title and text similarity.
5. **Fuzzy matching** — title-blocked pairs are compared with `difflib.SequenceMatcher`; those scoring ≥ 0.85 are grouped as duplicates.
6. **Extended edition detection** — if matched files share content but page counts differ by ≥ 15% and ≥ 30 pages, the group is flagged as `extended_version` (the longer file likely has a supplement or addendum added).

## Categories

`AI / Machine Learning` · `Programming` · `Web Development` · `DevOps / Cloud` · `Database` · `Data Science` · `Security / Hacking` · `Language / Linguistics` · `Mathematics` · `Science` · `Business / Finance` · `Health / Medicine` · `History / Philosophy` · `Literature / Fiction` · `Reference / Dictionary` · `Uncategorized`

## Database schema

```sql
-- Every indexed ebook
CREATE TABLE books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE NOT NULL,
    filename    TEXT NOT NULL,
    format      TEXT NOT NULL,   -- pdf, epub, djvu, mobi, chm, azw3
    size        INTEGER,
    sha256      TEXT,
    title       TEXT,
    author      TEXT,
    tags        TEXT,
    language    TEXT,
    publisher   TEXT,
    category    TEXT,
    scanned_at  TEXT
);

-- Files that share a (sha256, format) pair with at least one other file
CREATE TABLE duplicates (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256  TEXT NOT NULL,
    format  TEXT NOT NULL,
    path    TEXT NOT NULL
);
```

## Useful queries

```bash
# Summary — books per category
sqlite3 books.db "SELECT category, COUNT(*) n FROM books GROUP BY category ORDER BY n DESC;"

# Find a book by partial title
sqlite3 books.db "SELECT title, author, path FROM books WHERE title LIKE '%Python%';"

# All AI/ML books
sqlite3 books.db "SELECT title, author FROM books WHERE category='AI / Machine Learning' ORDER BY title;"

# List every duplicate group with all paths
sqlite3 books.db "
  SELECT sha256, format, path
  FROM   duplicates
  ORDER  BY sha256, format, path;"
```

## Duplicates workflow

`duplicates.txt` lists byte-identical duplicates grouped by hash:

```
### [PDF] sha256:04fffa05e…
  /data/pdf/biology/Mader Biology 10th txtbk.PDF
  /home/Incoming/Biology - Sylvia S. Mader - 10th ed, McGraw-Hill, 2010.pdf
```

`fuzzy_duplicates.txt` lists content-identical duplicates with the match reason:

```
### [EPUB] content_hash  sim=1.00  pages: 48–48  (hash:10fbb262…)
  /home/crt/.../Vibe Coding (Gene Kim, Steve Yegge).epub  [48 pp]
  /home/crt/.../Vibe Coding (Steve Yegge, Gene Kim).epub  [48 pp]

### [PDF] extended_version  sim=0.91  pages: 312–412  (hash:…)
  ← DO NOT delete the longer file without checking its extra pages
  /data/pdf/AlgoDesign-1st.pdf  [312 pp]
  /data/pdf/AlgoDesign-2nd.pdf  [412 pp]  ← extended/longer
```

Match reasons:
- `content_hash` — normalized text is byte-for-byte identical
- `isbn` — same validated ISBN, confirmed by title and text similarity
- `fuzzy_text` — text similarity ≥ 85%
- `extended_version` — same front matter, page counts differ significantly; review before deleting

Groups matched via OCR are tagged `[ocr]`.

> Some groups may include documentation PDFs duplicated across R/Conda environments (`/anaconda3/`, `/R/library/`). These are safe to ignore or delete.

## Keeping the database current

`updatedb` runs daily via cron, so simply re-run the scanner after new books are downloaded:

```bash
python3 ebook_scanner.py
```

To force a full rescan from scratch, delete `books.db` first.
