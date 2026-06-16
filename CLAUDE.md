# CLAUDE.md — Ebook Scanner

## What this project does

Single-file Python tool (`ebook_scanner.py`) that:
1. Finds all ebooks on the system via `plocate`
2. Extracts metadata with calibre's `ebook-meta`
3. Detects exact duplicates (SHA-256 hash per format)
4. Categorizes each book by keyword matching
5. Stores everything in `books.db` (SQLite)
6. Writes `duplicates.txt` and `report.txt`

## Running it

```bash
# Step 1: scan & index all ebooks
python3 ebook_scanner.py

# Step 2: find content-based duplicates (byte-different, same content)
python3 content_dedup.py
```

Both scripts are resumable — already-processed files are skipped.

## Key constants

### ebook_scanner.py

| Constant | Default | Purpose |
|---|---|---|
| `MAX_WORKERS` | 20 | Parallel `ebook-meta` + hash threads |
| `EXCLUDE_PATTERNS` | see file | Paths to skip (venvs, conda, wine, …) |
| `CATEGORIES` | see file | Ordered keyword→category rules |
| `CHUNK_SIZE` | 65536 | Read chunk for SHA-256 hashing |

## Output files (not committed to git)

- `books.db` — SQLite; tables `books`, `duplicates`, `fuzzy_duplicates`
- `duplicates.txt` — byte-identical duplicates grouped by SHA-256
- `fuzzy_duplicates.txt` — content-identical duplicates (different bytes, same text)
- `report.txt` — counts by format, category, and top authors

## Dependencies

- Python 3.10+ (stdlib only — no pip installs needed)
- `calibre` — provides `ebook-meta` (`dnf install calibre`)
- `plocate` — provides `locate` (`dnf install mlocate`)

### content_dedup.py

| Constant | Default | Purpose |
|---|---|---|
| `MAX_WORKERS` | 16 | Parallel extraction threads |
| `SAMPLE_CHARS` | 3000 | Characters extracted per book |
| `FUZZY_THRESH` | 0.85 | SequenceMatcher ratio to call a match |
| `ISBN_TEXT_SIM` | 0.75 | Min text similarity to confirm an ISBN match |
| `ISBN_TITLE_SIM` | 0.70 | Min title similarity to confirm an ISBN match |
| `MIN_CONTENT_LEN` | 200 | Min normalized chars; below this, no fingerprint is stored |
| `OCR_MAX_PAGES` | 6 | Max pages to OCR per book |
| `EXTENDED_PAGE_RATIO` | 1.15 | Page ratio threshold to flag as extended version |
| `EXTENDED_PAGE_DIFF` | 30 | Min page difference to flag as extended version |

Extraction tools: `pdftotext` (PDF, first 5 pages), `djvutxt` (DjVu, first 5 pages), `zipfile` + HTML stripping (EPUB spine order), `ebook-convert` (MOBI, AZW3, CHM).

**OCR fallback**: Image-based PDFs and DjVu files (where text extraction yields nothing) are processed with `tesseract 5.x`. Language is auto-detected from the DB language field, CJK characters in the file path, or path keywords (`/chinese/`, `/japanese/`, etc.). Script detection uses Unicode block analysis (no external libs). Installed language packs: `chi_sim, chi_tra, deu, eng, fra, jpn, kor, rus, spa`. Groups matched via OCR are tagged `[ocr]` in output.

Match reasons in `fuzzy_duplicates.txt`:
- `content_hash` — normalized extracted text is byte-for-byte identical
- `isbn` — checksummed ISBN found near "ISBN" label, confirmed by title + text similarity
- `fuzzy_text` — title-blocked pairs with SequenceMatcher ≥ 0.85
- `extended_version` — front matter matches but page counts differ by ≥15% and ≥30 pages; longer file likely has a supplement/addendum — **do not delete without reviewing**

## Extending categories

`CATEGORIES` in `ebook_scanner.py` is a list of `(label, [keywords])` tuples checked in order; first match wins. Add or reorder entries there. After editing, delete `books.db` and re-run to reclassify everything, or run a targeted SQL `UPDATE` for speed.

## Useful queries

```bash
# Books per category
sqlite3 books.db "SELECT category, COUNT(*) n FROM books GROUP BY category ORDER BY n DESC;"

# Search by title
sqlite3 books.db "SELECT title, author, path FROM books WHERE title LIKE '%Python%';"

# All duplicates
sqlite3 books.db "SELECT sha256, format, path FROM duplicates ORDER BY sha256, format;"
```

## Notes

- Some duplicate groups contain R/Conda package PDFs (paths contain `/anaconda3/`, `/R/library/`). These are documentation bundled with packages, not user books — safe to ignore.
- Non-UTF-8 filenames (e.g., Chinese characters in some older files) are handled via latin-1 fallback decoding.
- `ebook-meta` takes ~0.2 s per file; 20 workers process ~7,700 files in roughly 7 minutes.
