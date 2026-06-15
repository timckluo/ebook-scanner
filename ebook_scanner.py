#!/usr/bin/env python3
"""
ebook_scanner.py
Scans the system for ebooks, extracts metadata via calibre's ebook-meta,
stores results in SQLite, detects exact duplicates (same format + SHA-256),
and categorizes each book.

Output:
  books.db        — SQLite database
  duplicates.txt  — full paths of duplicate files (grouped by hash)
  report.txt      — summary statistics
"""

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Configuration ────────────────────────────────────────────────────────────
DB_PATH          = Path(__file__).parent / "books.db"
DUPLICATES_PATH  = Path(__file__).parent / "duplicates.txt"
REPORT_PATH      = Path(__file__).parent / "report.txt"

EBOOK_EXTENSIONS = {".pdf", ".epub", ".djvu", ".mobi", ".chm", ".azw3"}

EXCLUDE_PATTERNS = [
    "site-packages", ".venv", "mpl-data", "__pycache__",
    "/proc/", "/sys/", "/dev/", "/run/",
    "/.wine/", "/snap/", "/flatpak/",
]

MAX_WORKERS = 20   # parallel ebook-meta + hash workers
CHUNK_SIZE  = 65536

# ── Category keyword map (checked in order; first match wins) ────────────────
CATEGORIES = [
    ("AI / Machine Learning",    ["artificial intelligence", "machine learning", "deep learning",
                                   "neural network", "llm", "large language", "chatgpt",
                                   "generative ai", "transformer", "nlp", "natural language",
                                   "computer vision", "reinforcement learning", "langchain",
                                   "langgraph", "diffusion model", "stable diffusion",
                                   "gpt", "llama", "agents and applications"]),
    ("Programming",              ["python", "javascript", "java ", " c++", "rust ", "golang",
                                   "go lang", "ruby ", "swift ", "kotlin", "typescript", "php ",
                                   "scala ", "haskell", "programming", "coding", "developer",
                                   "software development", "algorithms", "data structures",
                                   "design patterns", "clean code", "refactoring"]),
    ("Web Development",          ["html", "css", "react", "angular", "vue.js", "node.js",
                                   "nodejs", "django", "flask", "fastapi", "web development",
                                   "frontend", "backend", "fullstack", "rest api", "graphql"]),
    ("DevOps / Cloud",           ["devops", "docker", "kubernetes", "aws", "azure", "google cloud",
                                   "cloud computing", "ci/cd", "jenkins", "ansible", "terraform",
                                   "linux", "freebsd", "unix", "system administration",
                                   "networking", "infrastructure", "devsecops"]),
    ("Database",                 ["mysql", "postgresql", "oracle ", "mongodb", "redis", "sqlite",
                                   " sql ", "nosql", "database", "data warehouse",
                                   "elasticsearch"]),
    ("Data Science",             ["data science", "data analysis", "pandas", "numpy",
                                   "statistics", "statistical", "r programming", "tableau",
                                   "power bi", "big data", "apache spark", "hadoop",
                                   "data engineering"]),
    ("Security / Hacking",       ["security", "hacking", "penetration testing", "cybersecurity",
                                   "ctf ", "malware", "cryptography", "encryption",
                                   "network security", "ethical hack", "exploit"]),
    ("Language / Linguistics",   ["linguistics", "phonology", "morphology", "syntax",
                                   "etymology", "dictionary", "grammar", "accent", "dialect",
                                   "southern min", "mandarin", "cantonese", "japanese",
                                   "latin ", "ancient greek", "old chinese", "translation",
                                   "vocabulary", "words "]),
    ("Mathematics",              ["mathematics", "calculus", "linear algebra", "topology",
                                   "number theory", "geometry", "probability", "theorem",
                                   "mathematical"]),
    ("Science",                  ["physics", "chemistry", "biology", "astronomy", "quantum",
                                   "thermodynamics", "mechanics", "genetics", "neuroscience",
                                   "cosmology"]),
    ("Business / Finance",       ["business", "finance", "investing", "stock market",
                                   "accounting", "management", "marketing",
                                   "entrepreneurship", "startup", "leadership",
                                   "economics", "cryptocurrency", "bitcoin", "blockchain"]),
    ("Health / Medicine",        ["health", "medicine", "medical", "anatomy", "physiology",
                                   "nutrition", "fitness", "exercise", "somatic", "therapy",
                                   "psychology", "mental health"]),
    ("History / Philosophy",     ["history", "philosophy", "ancient", "civilization",
                                   "medieval", "political theory", "ethics"]),
    ("Literature / Fiction",     ["novel", "fiction", "manga", "comic", "fantasy",
                                   "science fiction", "mystery", "thriller", "romance",
                                   "poetry", "short stories"]),
    ("Reference / Dictionary",   ["dictionary", "encyclopedia", "handbook", "supplement",
                                   "websters", "merriam"]),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def sha256_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
        return h.hexdigest()
    except (IOError, PermissionError, OSError):
        return None


def parse_meta_output(output: str) -> dict:
    """Parse calibre ebook-meta stdout into a dict."""
    meta = {"title": None, "author": None, "tags": None,
            "language": None, "publisher": None}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not val or val in ("None", "Unknown"):
            continue
        if key == "title":
            meta["title"] = val
        elif key == "author(s)":
            meta["author"] = val
        elif key == "tags":
            meta["tags"] = val
        elif key == "languages":
            meta["language"] = val
        elif key == "publisher":
            meta["publisher"] = val
    return meta


def parse_zlib_filename(stem: str) -> tuple[str | None, str | None]:
    """
    Many filenames follow the pattern:
      Title (Author Name) (Z-Library).ext
      Title (Author) (z-library.sk, 1lib.sk, z-lib.sk).ext
    Returns (title, author) or (None, None).
    """
    # Match: "Title (Author) (source-hint)"
    m = re.match(
        r"^(.+?)\s+\(([^)]+)\)\s+\([^)]*(?:z-lib|z-library|library)[^)]*\)\s*$",
        stem, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # Generic two-group: "Title (Author)"
    m = re.match(r"^(.+?)\s+\(([^)]+)\)\s*$", stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def categorize(title: str | None, filename: str, tags: str | None) -> str:
    text = " ".join(filter(None, [title, filename, tags])).lower()
    for cat, keywords in CATEGORIES:
        for kw in keywords:
            if kw in text:
                return cat
    return "Uncategorized"


def process_file(path: str) -> dict | None:
    """Hash + metadata for one file. Returns a record dict or None on error."""
    p = Path(path)
    if not p.is_file():
        return None
    ext = p.suffix.lower()
    if ext not in EBOOK_EXTENSIONS:
        return None

    size   = p.stat().st_size
    sha    = sha256_file(path)
    fmt    = ext.lstrip(".")

    # Try ebook-meta first
    meta = {"title": None, "author": None, "tags": None,
            "language": None, "publisher": None}
    try:
        res = subprocess.run(
            ["ebook-meta", path],
            capture_output=True, text=True, timeout=30,
        )
        meta = parse_meta_output(res.stdout)
    except Exception:
        pass

    # Fall back to filename parsing when ebook-meta found nothing
    if not meta["title"]:
        t, a = parse_zlib_filename(p.stem)
        if t:
            meta["title"]  = meta["title"]  or t
            meta["author"] = meta["author"] or a

    title    = meta["title"] or p.stem
    category = categorize(title, p.name, meta.get("tags"))

    return {
        "path":      path,
        "filename":  p.name,
        "format":    fmt,
        "size":      size,
        "sha256":    sha,
        "title":     meta["title"],
        "author":    meta["author"],
        "tags":      meta["tags"],
        "language":  meta["language"],
        "publisher": meta["publisher"],
        "category":  category,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Database ─────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    UNIQUE NOT NULL,
    filename    TEXT    NOT NULL,
    format      TEXT    NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_sha256_format ON books(sha256, format);
CREATE INDEX IF NOT EXISTS idx_format        ON books(format);
CREATE INDEX IF NOT EXISTS idx_category      ON books(category);
CREATE INDEX IF NOT EXISTS idx_author        ON books(author);

CREATE TABLE IF NOT EXISTS duplicates (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256  TEXT    NOT NULL,
    format  TEXT    NOT NULL,
    path    TEXT    NOT NULL,
    UNIQUE(sha256, path)
);
"""

INSERT_SQL = """
INSERT OR REPLACE INTO books
  (path, filename, format, size, sha256, title, author, tags,
   language, publisher, category, scanned_at)
VALUES
  (:path, :filename, :format, :size, :sha256, :title, :author, :tags,
   :language, :publisher, :category, :scanned_at)
"""


def init_db(conn: sqlite3.Connection):
    conn.executescript(DDL)
    conn.commit()


def upsert_books(conn: sqlite3.Connection, records: list[dict]):
    conn.executemany(INSERT_SQL, records)
    conn.commit()


def find_and_store_duplicates(conn: sqlite3.Connection):
    conn.execute("DELETE FROM duplicates")
    conn.execute("""
        INSERT INTO duplicates (sha256, format, path)
        SELECT b.sha256, b.format, b.path
        FROM   books b
        WHERE  b.sha256 IS NOT NULL
          AND  (b.sha256, b.format) IN (
                 SELECT sha256, format
                 FROM   books
                 WHERE  sha256 IS NOT NULL
                 GROUP  BY sha256, format
                 HAVING COUNT(*) > 1
               )
        ORDER  BY b.sha256, b.format, b.path
    """)
    conn.commit()


# ── File discovery ────────────────────────────────────────────────────────────

def find_ebook_paths() -> list[str]:
    """
    Use plocate to enumerate all ebook paths, then filter out
    library/venv/system noise.  Handles arbitrary byte sequences in filenames.
    """
    raw_lines: list[bytes] = []

    try:
        res = subprocess.run(
            ["locate", "--regex", r"\.(pdf|epub|djvu|mobi|chm|azw3)$"],
            capture_output=True, timeout=60,
        )
        raw_lines = res.stdout.splitlines()
    except Exception as exc:
        print(f"[warn] locate failed: {exc}; falling back to find")

    if not raw_lines:
        # Fallback: use find with -print0 for safe NUL-delimited output
        search_roots = [r for r in ("/home", "/root", "/data", "/mnt", "/media", "/VM")
                        if os.path.isdir(r)]
        exts = " -o ".join(f"-name '*.{e.lstrip('.')}'" for e in
                           ("pdf", "epub", "djvu", "mobi", "chm", "azw3"))
        for root in search_roots:
            try:
                res = subprocess.run(
                    f"find {root} \\( {exts} \\) -print0 2>/dev/null",
                    shell=True, capture_output=True, timeout=120,
                )
                raw_lines.extend(
                    line for line in res.stdout.split(b"\x00") if line
                )
            except Exception:
                pass

    paths = []
    for raw in raw_lines:
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            line = raw.decode("latin-1").strip()
        if not line:
            continue
        if any(ex in line for ex in EXCLUDE_PATTERNS):
            continue
        try:
            ext = Path(line).suffix.lower()
        except Exception:
            continue
        if ext in EBOOK_EXTENSIONS:
            paths.append(line)

    return sorted(set(paths))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now():%H:%M:%S}] Starting ebook scanner …")

    # ── Discover files ──
    print("[…] Discovering ebook files via locate …")
    all_paths = find_ebook_paths()
    print(f"    Found {len(all_paths)} candidate files.")

    # ── Open DB and skip already-processed files ──
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    existing = {row[0] for row in conn.execute("SELECT path FROM books")}
    new_paths = [p for p in all_paths if p not in existing]
    print(f"    {len(existing)} already in DB, {len(new_paths)} new to process.")

    # ── Process files in parallel ──
    total   = len(new_paths)
    done    = 0
    batch   = []
    BATCH_N = 200  # flush to DB every N records

    print(f"[…] Processing {total} files with {MAX_WORKERS} workers …")

    def flush(records):
        upsert_books(conn, records)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(process_file, p): p for p in new_paths}
        for fut in as_completed(futures):
            done += 1
            if done % 500 == 0 or done == total:
                pct = done / total * 100 if total else 100
                print(f"    {done}/{total} ({pct:.1f}%) …", flush=True)
            try:
                rec = fut.result()
            except Exception as exc:
                print(f"    [warn] {futures[fut]}: {exc}", file=sys.stderr)
                continue
            if rec:
                batch.append(rec)
            if len(batch) >= BATCH_N:
                flush(batch)
                batch.clear()

    if batch:
        flush(batch)

    # ── Detect duplicates ──
    print("[…] Detecting duplicates …")
    find_and_store_duplicates(conn)

    dup_count = conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
    dup_groups = conn.execute(
        "SELECT COUNT(DISTINCT sha256||format) FROM duplicates"
    ).fetchone()[0]

    # ── Write duplicates.txt ──
    print(f"[…] Writing duplicate list → {DUPLICATES_PATH}")
    with open(DUPLICATES_PATH, "w") as f:
        f.write(f"# Duplicate ebooks — generated {datetime.now()}\n")
        f.write(f"# {dup_count} files in {dup_groups} duplicate groups\n")
        f.write("# Files in the same group are byte-for-byte identical (same format)\n\n")

        cur_group = None
        for sha256, fmt, path in conn.execute(
            "SELECT sha256, format, path FROM duplicates ORDER BY sha256, format, path"
        ):
            group_key = (sha256, fmt)
            if group_key != cur_group:
                if cur_group is not None:
                    f.write("\n")
                cur_group = group_key
                f.write(f"### [{fmt.upper()}] sha256:{sha256}\n")
            f.write(f"  {path}\n")

    # ── Write report.txt ──
    print(f"[…] Writing report → {REPORT_PATH}")
    total_books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]

    with open(REPORT_PATH, "w") as f:
        f.write(f"Ebook Scanner Report — {datetime.now()}\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Total books indexed : {total_books}\n")
        f.write(f"Duplicate files     : {dup_count} ({dup_groups} groups)\n\n")

        f.write("─── By Format ───\n")
        for fmt, cnt in conn.execute(
            "SELECT format, COUNT(*) n FROM books GROUP BY format ORDER BY n DESC"
        ):
            f.write(f"  {fmt:<8} {cnt}\n")

        f.write("\n─── By Category ───\n")
        for cat, cnt in conn.execute(
            "SELECT category, COUNT(*) n FROM books GROUP BY category ORDER BY n DESC"
        ):
            f.write(f"  {cnt:>5}  {cat}\n")

        f.write("\n─── Top Authors (by book count) ───\n")
        for author, cnt in conn.execute(
            "SELECT author, COUNT(*) n FROM books WHERE author IS NOT NULL "
            "GROUP BY author ORDER BY n DESC LIMIT 20"
        ):
            f.write(f"  {cnt:>5}  {author}\n")

    conn.close()
    print(f"\n[{datetime.now():%H:%M:%S}] Done.")
    print(f"  Database   : {DB_PATH}")
    print(f"  Duplicates : {DUPLICATES_PATH}  ({dup_count} files in {dup_groups} groups)")
    print(f"  Report     : {REPORT_PATH}")


if __name__ == "__main__":
    main()
