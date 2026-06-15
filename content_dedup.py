#!/usr/bin/env python3
"""
content_dedup.py
Finds ebooks whose content is the same even when their bytes differ.

Strategy (applied per format):
  1. Content-hash match  — same normalized extracted text → identical content
  2. ISBN match          — same ISBN number found in copyright pages
  3. Fuzzy-text match    — SequenceMatcher ratio ≥ 0.85 within title-blocked groups

Extraction tools:
  PDF   → pdftotext (first 5 pages)
  DjVu  → djvutxt   (first 5 pages)
  EPUB  → zipfile + HTML stripping (spine order, first 3000 chars)
  MOBI / AZW3 / CHM → calibre ebook-convert → txt (first 3000 chars)

New DB columns added to 'books': content_sample, content_hash, isbn, pub_year
New DB table: fuzzy_duplicates
New output file: fuzzy_duplicates.txt
"""

import difflib
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

DB_PATH      = Path(__file__).parent / "books.db"
OUT_PATH     = Path(__file__).parent / "fuzzy_duplicates.txt"
MAX_WORKERS  = 16
SAMPLE_CHARS = 3000   # characters to extract per book
FUZZY_THRESH = 0.85   # SequenceMatcher ratio threshold


# ── Text extraction ───────────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', raw)
    return re.sub(r'\s+', ' ', text).strip()


def _run(cmd: list, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout.decode('utf-8', errors='replace')
    except Exception:
        return ''


def extract_pdf(path: str) -> str:
    return _run(['pdftotext', '-l', '5', path, '-'])


def extract_djvu(path: str) -> str:
    return _run(['djvutxt', '--page=1-5', path])


def extract_epub(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            # Resolve spine order via OPF
            files = []
            try:
                container = z.read('META-INF/container.xml').decode('utf-8', errors='replace')
                m = re.search(r'full-path="([^"]+)"', container)
                if m:
                    opf_path = m.group(1)
                    opf_dir  = opf_path.rsplit('/', 1)[0] if '/' in opf_path else ''
                    opf      = z.read(opf_path).decode('utf-8', errors='replace')
                    manifest = {i: h for i, h in re.findall(
                        r'<item\b[^>]*\bid="([^"]*)"[^>]*\bhref="([^"]*)"', opf)}
                    for idref in re.findall(r'<itemref\b[^>]*\bidref="([^"]*)"', opf):
                        href = manifest.get(idref, '')
                        if href and href.lower().endswith(('.html', '.htm', '.xhtml')):
                            fp = (opf_dir + '/' + href).lstrip('/') if opf_dir else href
                            if fp in names:
                                files.append(fp)
            except Exception:
                pass
            if not files:
                files = sorted(n for n in names
                               if n.lower().endswith(('.html', '.htm', '.xhtml')))
            chunks, collected = [], 0
            for fp in files[:8]:
                if collected >= SAMPLE_CHARS:
                    break
                raw  = z.read(fp).decode('utf-8', errors='replace')
                text = _strip_html(raw)
                chunks.append(text)
                collected += len(text)
            return ' '.join(chunks)[:SAMPLE_CHARS]
    except Exception:
        return ''


def extract_calibre(path: str) -> str:
    with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tf:
        tmp = tf.name
    try:
        subprocess.run(
            ['ebook-convert', path, tmp, '--output-profile=default'],
            capture_output=True, timeout=60,
        )
        with open(tmp, encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:
        return ''
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


EXTRACTORS = {
    'pdf':  extract_pdf,
    'djvu': extract_djvu,
    'epub': extract_epub,
    'mobi': extract_calibre,
    'azw3': extract_calibre,
    'chm':  extract_calibre,
}


# ── Normalisation & fingerprinting ────────────────────────────────────────────

# Must appear near an "ISBN" label to reduce accidental matches
_ISBN_CONTEXT_RE = re.compile(
    r'isbn[\s:.\-]{0,4}'
    r'((?:97[89])[\s\-]?(?:\d[\s\-]?){9}\d'   # ISBN-13
    r'|\d[\s\-]?(?:\d[\s\-]?){7,8}[\dX])',     # ISBN-10
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r'(?:©|[Cc]opyright)\s*(?:©)?\s*(\d{4})')

MIN_CONTENT_LEN = 200  # minimum normalized chars to trust a content fingerprint


def _isbn10_valid(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = sum((i + 1) * (10 if d == 'X' else int(d))
                for i, d in enumerate(digits))
    return total % 11 == 0


def _isbn13_valid(digits: str) -> bool:
    if len(digits) != 13:
        return False
    total = sum((3 if i % 2 else 1) * int(d) for i, d in enumerate(digits))
    return total % 10 == 0


def normalize(text: str) -> str:
    """Lowercase, strip control chars, collapse whitespace."""
    text = text.lower()
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_isbn(text: str) -> str | None:
    """Return first checksummed ISBN-10/13 found near an 'ISBN' label."""
    for m in _ISBN_CONTEXT_RE.finditer(text):
        raw    = m.group(1)
        digits = re.sub(r'[\s\-]', '', raw).upper()
        if len(digits) == 13 and _isbn13_valid(digits):
            return digits
        if len(digits) == 10 and _isbn10_valid(digits):
            return digits
    return None


def extract_year(text: str) -> str | None:
    m = _YEAR_RE.search(text)
    return m.group(1) if m else None


def content_hash(text: str) -> str | None:
    n = normalize(text)
    if len(n) < MIN_CONTENT_LEN:
        return None
    return hashlib.sha256(n.encode()).hexdigest()


# ── Per-book processing ───────────────────────────────────────────────────────

def process_book(row: tuple) -> dict:
    """Extract content fingerprint for one book row from the DB."""
    book_id, path, fmt = row
    extractor = EXTRACTORS.get(fmt)
    raw = extractor(path) if extractor and os.path.isfile(path) else ''
    sample = raw[:SAMPLE_CHARS] if raw else ''
    return {
        'id':             book_id,
        'content_sample': sample or None,
        'content_hash':   content_hash(sample) if sample else None,
        'isbn':           extract_isbn(sample) if sample else None,
        'pub_year':       extract_year(sample) if sample else None,
    }


# ── Database helpers ──────────────────────────────────────────────────────────

FUZZY_DDL = """
CREATE TABLE IF NOT EXISTS fuzzy_duplicates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT    NOT NULL,
    format     TEXT    NOT NULL,
    path       TEXT    NOT NULL,
    reason     TEXT,
    similarity REAL,
    UNIQUE(group_id, path)
);
CREATE INDEX IF NOT EXISTS idx_fdgroup ON fuzzy_duplicates(group_id);
CREATE INDEX IF NOT EXISTS idx_content_hash ON books(content_hash);
CREATE INDEX IF NOT EXISTS idx_isbn          ON books(isbn);
"""


def upgrade_schema(conn: sqlite3.Connection):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
    for col, defn in [
        ('content_sample', 'TEXT'),
        ('content_hash',   'TEXT'),
        ('isbn',           'TEXT'),
        ('pub_year',       'TEXT'),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {defn}")
    conn.executescript(FUZZY_DDL)
    conn.commit()


def upsert_content(conn: sqlite3.Connection, records: list[dict]):
    conn.executemany("""
        UPDATE books
        SET content_sample = :content_sample,
            content_hash   = :content_hash,
            isbn           = :isbn,
            pub_year       = :pub_year
        WHERE id = :id
    """, records)
    conn.commit()


# ── Duplicate-finding logic ───────────────────────────────────────────────────

def _title_key(title: str | None, filename: str) -> str:
    """Four-word normalized key used for blocking comparisons."""
    text = title or Path(filename).stem
    words = normalize(text).split()
    # drop very short tokens (articles, prepositions)
    sig = [w for w in words if len(w) > 2][:4]
    return ' '.join(sig)


def find_fuzzy_duplicates(conn: sqlite3.Connection) -> list[tuple]:
    """
    Returns a list of (group_id, format, path, reason, similarity) tuples.
    Groups are formed by:
      1. content_hash — same extracted-text hash (identical content)
      2. isbn         — same ISBN number
      3. fuzzy_text   — SequenceMatcher ratio ≥ FUZZY_THRESH within title blocks
    """
    rows = conn.execute("""
        SELECT id, path, filename, format, title, content_hash, isbn, content_sample
        FROM   books
        WHERE  content_sample IS NOT NULL
          AND  length(content_sample) >= 200
        ORDER  BY format, path
    """).fetchall()

    groups: dict[str, list] = {}   # group_id → list of (path, format, reason, sim)

    # ── Pass 1: content_hash ──────────────────────────────────────────────────
    from collections import defaultdict
    hash_buckets: dict[tuple, list] = defaultdict(list)
    for _id, path, fname, fmt, title, chash, isbn, sample in rows:
        if chash:
            hash_buckets[(fmt, chash)].append(path)

    for (fmt, chash), paths in hash_buckets.items():
        if len(paths) > 1:
            gid = f"hash:{chash[:16]}"
            groups[gid] = [(p, fmt, 'content_hash', 1.0) for p in paths]

    # ── Pass 2: ISBN (confirmed by minimum text similarity) ───────────────────
    # Build a sample lookup for quick access
    sample_by_path = {path: sample for _, path, _, _, _, _, _, sample in rows}

    isbn_buckets: dict[tuple, list] = defaultdict(list)
    for _id, path, fname, fmt, title, chash, isbn, sample in rows:
        if isbn and len(isbn) >= 10:
            isbn_buckets[(fmt, isbn)].append(path)

    ISBN_TEXT_SIM  = 0.75  # text similarity threshold for ISBN confirmation
    ISBN_TITLE_SIM = 0.70  # if both books have DB titles, they must be this similar

    # Build title lookup
    title_by_path = {path: (title or '') for _, path, _, _, title, _, _, _ in rows}

    for (fmt, isbn), paths in isbn_buckets.items():
        if len(paths) < 2:
            continue
        existing_paths = {p for g in groups.values() for p, *_ in g}
        novel = [p for p in paths if p not in existing_paths]
        if len(novel) < 2:
            continue
        # Pairwise check: text similarity AND title similarity (if both have titles)
        confirmed: set[str] = set()
        for i in range(len(novel)):
            for j in range(i + 1, len(novel)):
                p1, p2 = novel[i], novel[j]
                # Title guard: if both books have titles, they must look alike
                t1 = normalize(title_by_path.get(p1, ''))
                t2 = normalize(title_by_path.get(p2, ''))
                if t1 and t2:
                    title_ratio = difflib.SequenceMatcher(None, t1, t2, autojunk=False).ratio()
                    if title_ratio < ISBN_TITLE_SIM:
                        continue  # different-title books sharing a series/publisher ISBN
                # Text content check
                s1 = normalize(sample_by_path.get(p1, ''))[:2000]
                s2 = normalize(sample_by_path.get(p2, ''))[:2000]
                if not s1 or not s2:
                    continue
                ratio = difflib.SequenceMatcher(None, s1, s2, autojunk=False).ratio()
                if ratio >= ISBN_TEXT_SIM:
                    confirmed.add(p1)
                    confirmed.add(p2)
        if len(confirmed) > 1:
            gid = f"isbn:{isbn}"
            groups[gid] = [(p, fmt, 'isbn', 0.99) for p in confirmed]

    # ── Pass 3: fuzzy text within title-blocked groups ───────────────────────
    already_grouped = {p for g in groups.values() for p, *_ in g}

    # Build per-format title-key buckets of books not yet grouped
    title_buckets: dict[tuple, list] = defaultdict(list)
    for _id, path, fname, fmt, title, chash, isbn, sample in rows:
        if path in already_grouped or not sample:
            continue
        key = _title_key(title, fname)
        if key and len(key) > 5:  # skip meaningless keys
            title_buckets[(fmt, key)].append((path, sample))

    gid_counter = 0
    for (fmt, key), book_list in title_buckets.items():
        if len(book_list) < 2:
            continue
        # Pairwise comparison within the bucket
        matched: dict[str, set] = {}  # path → set of matching paths
        for i in range(len(book_list)):
            for j in range(i + 1, len(book_list)):
                p1, s1 = book_list[i]
                p2, s2 = book_list[j]
                ratio = difflib.SequenceMatcher(None,
                    normalize(s1)[:2000], normalize(s2)[:2000],
                    autojunk=False).ratio()
                if ratio >= FUZZY_THRESH:
                    matched.setdefault(p1, set()).add(p2)
                    matched.setdefault(p2, set()).add(p1)

        # Union-find to merge transitively connected books
        visited = set()
        for path, peers in matched.items():
            if path in visited:
                continue
            # BFS
            cluster = set()
            queue = [path]
            while queue:
                cur = queue.pop()
                if cur in cluster:
                    continue
                cluster.add(cur)
                visited.add(cur)
                queue.extend(matched.get(cur, set()) - cluster)
            if len(cluster) > 1:
                gid_counter += 1
                gid = f"fuzzy:{fmt}:{gid_counter:04d}"
                # Compute best similarity score per path
                entries = []
                for p in cluster:
                    best = max(
                        (difflib.SequenceMatcher(None,
                            normalize(s), normalize(book_list[k][1])[:2000],
                            autojunk=False).ratio()
                         for k, (bp, s) in enumerate(book_list) if bp in cluster and bp != p),
                        default=0.0,
                    )
                    entries.append((p, fmt, 'fuzzy_text', round(best, 3)))
                groups[gid] = entries

    # Flatten to list of tuples
    result = []
    for gid, entries in groups.items():
        for path, fmt, reason, sim in entries:
            result.append((gid, fmt, path, reason, sim))
    return result


# ── Output ────────────────────────────────────────────────────────────────────

def write_output(conn: sqlite3.Connection, records: list[tuple]):
    from collections import defaultdict
    by_group: dict[str, list] = defaultdict(list)
    for gid, fmt, path, reason, sim in records:
        by_group[gid].append((fmt, path, reason, sim))

    reason_order = {'content_hash': 0, 'isbn': 1, 'fuzzy_text': 2}
    sorted_groups = sorted(by_group.items(),
                           key=lambda kv: reason_order.get(kv[1][0][2], 9))

    hash_groups  = sum(1 for g in by_group.values() if g[0][2] == 'content_hash')
    isbn_groups  = sum(1 for g in by_group.values() if g[0][2] == 'isbn')
    fuzzy_groups = sum(1 for g in by_group.values() if g[0][2] == 'fuzzy_text')
    total_files  = len(records)

    with open(OUT_PATH, 'w') as f:
        f.write(f"# Content-based duplicate ebooks — {datetime.now()}\n")
        f.write(f"# {total_files} files across {len(by_group)} groups\n")
        f.write(f"# content_hash:{hash_groups}  isbn:{isbn_groups}  fuzzy_text:{fuzzy_groups}\n")
        f.write("#\n")
        f.write("# Match reasons:\n")
        f.write("#   content_hash — byte-different files with identical extracted text\n")
        f.write("#   isbn         — same ISBN number found in copyright pages\n")
        f.write("#   fuzzy_text   — text similarity ≥ 85% (same content, different encoding/watermark)\n\n")

        for gid, entries in sorted_groups:
            fmt    = entries[0][0]
            reason = entries[0][2]
            sim    = max(e[3] for e in entries)
            f.write(f"### [{fmt.upper()}] {reason}  sim={sim:.2f}  ({gid})\n")
            for _, path, _, esim in sorted(entries, key=lambda x: x[1]):
                f.write(f"  {path}\n")
            f.write('\n')

    return hash_groups, isbn_groups, fuzzy_groups, len(by_group)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now():%H:%M:%S}] Content-based deduplication …")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    upgrade_schema(conn)

    # Which books still need content extraction?
    pending = conn.execute("""
        SELECT id, path, format FROM books
        WHERE  content_sample IS NULL
    """).fetchall()
    print(f"    {len(pending)} books need content extraction "
          f"({conn.execute('SELECT COUNT(*) FROM books WHERE content_sample IS NOT NULL').fetchone()[0]} already done)")

    if pending:
        done, total, batch = 0, len(pending), []
        BATCH_N = 100

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(process_book, row): row for row in pending}
            for fut in as_completed(futures):
                done += 1
                if done % 500 == 0 or done == total:
                    print(f"    {done}/{total} ({done/total*100:.1f}%) …", flush=True)
                try:
                    rec = fut.result()
                    batch.append(rec)
                except Exception as exc:
                    print(f"    [warn] {futures[fut][1]}: {exc}", file=sys.stderr)
                if len(batch) >= BATCH_N:
                    upsert_content(conn, batch)
                    batch.clear()
        if batch:
            upsert_content(conn, batch)

        extracted = conn.execute(
            "SELECT COUNT(*) FROM books WHERE content_sample IS NOT NULL"
        ).fetchone()[0]
        print(f"    Extraction done: {extracted} books have content samples.")

    # Find duplicates
    print("[…] Finding content-based duplicates …")
    conn.execute("DELETE FROM fuzzy_duplicates")
    records = find_fuzzy_duplicates(conn)

    if records:
        conn.executemany("""
            INSERT OR IGNORE INTO fuzzy_duplicates (group_id, format, path, reason, similarity)
            VALUES (?,?,?,?,?)
        """, records)
        conn.commit()

    print(f"[…] Writing → {OUT_PATH}")
    h, i, f_cnt, g = write_output(conn, records)

    conn.close()
    print(f"\n[{datetime.now():%H:%M:%S}] Done.")
    print(f"  {g} duplicate groups  "
          f"(content_hash:{h}  isbn:{i}  fuzzy_text:{f_cnt})")
    print(f"  {len(records)} total files listed in {OUT_PATH}")


if __name__ == '__main__':
    main()
