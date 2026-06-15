# Ebook Scanner

Scans the entire system for ebooks, extracts metadata, detects exact duplicates, and categorizes every book into a SQLite database.

## Requirements

- Python 3.10+
- [Calibre](https://calibre-ebook.com/) (`ebook-meta` must be on `$PATH`)
- `plocate` / `locate` (for fast system-wide file discovery)

All three are standard on Fedora:
```bash
dnf install calibre mlocate
```

## Usage

```bash
python3 ebook_scanner.py
```

The script is **resumable** — files already in the database are skipped, so re-running after adding new books (or after `updatedb`) only processes the new files.

## Output files

| File | Description |
|---|---|
| `books.db` | SQLite database of all indexed ebooks |
| `duplicates.txt` | Duplicate files grouped by SHA-256 hash |
| `report.txt` | Summary statistics (format, category, top authors) |

## Supported formats

`.pdf` · `.epub` · `.djvu` · `.mobi` · `.chm` · `.azw3`

## How it works

1. **Discovery** — runs `locate --regex` to find all ebook files system-wide, filtering out Python venvs, Wine prefixes, Conda environments, and other system noise.
2. **Hashing** — computes a SHA-256 hash of each file's raw bytes.
3. **Metadata** — calls `calibre`'s `ebook-meta` on every file to extract title, author, publisher, language, and tags. Falls back to parsing the filename (handles the common Z-Library `Title (Author) (Z-Library).ext` pattern).
4. **Categorization** — assigns each book to one of 15 categories based on keyword matching against the title, filename, and tags.
5. **Deduplication** — any two files with the same SHA-256 hash *and* the same format are considered exact duplicates and written to `duplicates.txt`.
6. **Storage** — all records are upserted into `books.db` (SQLite with WAL mode).

Processing is parallelized across 20 workers; ~7,700 files take roughly 7 minutes.

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

`duplicates.txt` lists every exact duplicate grouped by hash:

```
### [PDF] sha256:04fffa05e…
  /data/pdf/biology/Mader Biology 10th txtbk.PDF
  /home/Incoming/Biology - Sylvia S. Mader - 10th ed, McGraw-Hill, 2010.pdf
```

Review the file, decide which copy to keep, and delete the others manually.

> Some groups may include documentation PDFs duplicated across R/Conda environments (`/anaconda3/`, `/R/library/`). These are safe to ignore or delete.

## Keeping the database current

`updatedb` runs daily via cron, so simply re-run the scanner after new books are downloaded:

```bash
python3 ebook_scanner.py
```

To force a full rescan from scratch, delete `books.db` first.
