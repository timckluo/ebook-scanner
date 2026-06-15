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
python3 ebook_scanner.py
```

The script is resumable — files already in `books.db` are skipped. Re-run freely after adding books or after `updatedb` runs.

## Key constants (top of ebook_scanner.py)

| Constant | Default | Purpose |
|---|---|---|
| `MAX_WORKERS` | 20 | Parallel `ebook-meta` + hash threads |
| `EXCLUDE_PATTERNS` | see file | Paths to skip (venvs, conda, wine, …) |
| `CATEGORIES` | see file | Ordered keyword→category rules |
| `CHUNK_SIZE` | 65536 | Read chunk for SHA-256 hashing |

## Output files (not committed to git)

- `books.db` — SQLite; tables `books` and `duplicates`
- `duplicates.txt` — one group per SHA-256, listing all duplicate paths
- `report.txt` — counts by format, category, and top authors

## Dependencies

- Python 3.10+ (stdlib only — no pip installs needed)
- `calibre` — provides `ebook-meta` (`dnf install calibre`)
- `plocate` — provides `locate` (`dnf install mlocate`)

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
