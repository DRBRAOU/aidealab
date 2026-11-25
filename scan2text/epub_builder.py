"""Turn OCR'd Markdown pages into a clean reflowable EPUB, or page images
into a fixed-layout EPUB."""
import re
import uuid
from collections import Counter

import markdown
from ebooklib import epub

CSS = """
body { font-family: serif; line-height: 1.5; margin: 0 5%; }
:lang(te) { font-family: "Noto Serif Telugu", "Noto Sans Telugu", "Nirmala UI", "Gautami", "Ramabhadra", serif; line-height: 1.8; }
:lang(hi) { font-family: "Noto Serif Devanagari", "Noto Sans Devanagari", "Nirmala UI", "Mangal", "Kokila", serif; line-height: 1.8; }
:lang(te) p, :lang(hi) p { text-align: left; }
h1 { font-size: 1.6em; text-align: center; margin: 2em 0 1em; page-break-before: always; }
h2 { font-size: 1.3em; margin: 1.5em 0 0.6em; }
h3 { font-size: 1.1em; margin: 1.2em 0 0.5em; }
p  { margin: 0; text-indent: 1.5em; text-align: justify; }
h1 + p, h2 + p, h3 + p, blockquote p { text-indent: 0; }
blockquote { margin: 1em 2em; font-style: italic; }
table { border-collapse: collapse; margin: 1em auto; }
td, th { border: 1px solid #999; padding: 0.2em 0.5em; }
"""

# ---------------- cleaning ----------------
_PAGE_NUM = re.compile(r"^\s*(page\s*)?[\divxlc]{1,6}\s*$", re.I)
_FENCE = re.compile(r"^```[a-z]*\s*\n?|\n?```\s*$", re.I)


def _norm(line):
    return re.sub(r"[\d\W_]+", "", line.lower())


def clean_pages(pages):
    """pages: list of markdown strings (one per scanned page) -> one markdown doc."""
    pages = [_FENCE.sub("", p.strip()).strip() for p in pages]
    split = [[l.rstrip() for l in p.splitlines()] for p in pages]

    # find running headers/footers: first/last lines that repeat on many pages
    edge = Counter()
    for lines in split:
        nonblank = [l for l in lines if l.strip()]
        for l in set(nonblank[:2] + nonblank[-2:]):
            if _norm(l):
                edge[_norm(l)] += 1
    threshold = max(3, len(pages) * 0.25)
    running = {k for k, n in edge.items() if n >= threshold and len(k) < 80}

    cleaned = []
    for lines in split:
        nonblank_idx = [i for i, l in enumerate(lines) if l.strip()]
        edges = set(nonblank_idx[:2] + nonblank_idx[-2:])
        keep = []
        for i, l in enumerate(lines):
            if i in edges and (_PAGE_NUM.match(l) or _norm(l) in running):
                continue
            keep.append(l)
        text = "\n".join(keep).strip()
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate line breaks
        cleaned.append(text)

    # stitch pages: continue a paragraph that was cut by a page break
    out = ""
    for text in cleaned:
        if not text:
            continue
        if not out:
            out = text
            continue
        last = out.rstrip()
        first = text.lstrip()
        # Telugu has no capital letters, so "not a capital" is the continuation test
        continues = bool(first) and not first[0].isupper() and first[0] not in "#|-*>[0123456789"
        if last.endswith("-") and first[:1].islower():
            out = last[:-1] + first
        elif (last[-1:] not in ".!?:\"'”’)|।॥" and not last.splitlines()[-1].startswith(("#", "|", "-", "*", ">"))
              and continues):
            out = last + " " + first
        else:
            out = last + "\n\n" + first
    return out


# ---------------- reflowable EPUB ----------------
def _split_chapters(md_text):
    for level in ("# ", "## "):
        parts = re.split(rf"(?m)^(?={re.escape(level)})", md_text)
        parts = [p for p in parts if p.strip()]
        if len(parts) > 1:
            return parts
    return [md_text]


def build_reflowable(md_text, title, author, lang="en", cover_png=None):
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    if cover_png:
        book.set_cover("cover.png", cover_png)

    # EPUB Accessibility 1.1 metadata (read by screen readers & stores)
    for prop, val in [
        ("schema:accessMode", "textual"),
        ("schema:accessModeSufficient", "textual"),
        ("schema:accessibilityFeature", "structuralNavigation"),
        ("schema:accessibilityFeature", "tableOfContents"),
        ("schema:accessibilityFeature", "readingOrder"),
        ("schema:accessibilityHazard", "none"),
        ("schema:accessibilitySummary",
         "Text converted from a scanned book with OCR. Headings, lists and tables "
         "are marked up for screen readers and the table of contents links every chapter."),
    ]:
        book.add_metadata(None, "meta", val, {"property": prop})

    css = epub.EpubItem(uid="css", file_name="style.css", media_type="text/css", content=CSS)
    book.add_item(css)

    chapters = []
    for i, part in enumerate(_split_chapters(md_text), start=1):
        m = re.match(r"#{1,2}\s+(.+)", part.strip())
        name = m.group(1).strip(" *") if m else (title if i == 1 else f"Part {i}")
        html = markdown.markdown(part, extensions=["tables", "sane_lists"])
        ch = epub.EpubHtml(title=name, file_name=f"ch{i:03}.xhtml", lang=lang)
        ch.content = html
        ch.add_item(css)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    return _write(book)


# ---------------- fixed-layout EPUB ----------------
def build_fixed(page_images, title, author, lang="en"):
    """page_images: list of (jpeg_bytes, width_px, height_px)."""
    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    book.add_metadata(None, "meta", "pre-paginated", {"property": "rendition:layout"})
    book.add_metadata(None, "meta", "auto", {"property": "rendition:spread"})

    pages = []
    for i, (jpg, w, h) in enumerate(page_images, start=1):
        img_name = f"images/p{i:04}.jpg"
        if i == 1:
            book.set_cover(img_name, jpg, create_page=False)
        else:
            book.add_item(epub.EpubItem(uid=f"img{i}", file_name=img_name,
                                        media_type="image/jpeg", content=jpg))
        # raw XHTML item (EpubHtml would strip the required viewport meta)
        xhtml = (f'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                 f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">'
                 f'<head><title>Page {i}</title>'
                 f'<meta name="viewport" content="width={w}, height={h}"/>'
                 f'<style>body{{margin:0;padding:0}}img{{display:block;width:{w}px;height:{h}px}}</style>'
                 f'</head><body><img src="{img_name}" alt="Page {i}"/></body></html>')
        pg = epub.EpubItem(uid=f"page{i}", file_name=f"p{i:04}.xhtml",
                           media_type="application/xhtml+xml", content=xhtml.encode())
        book.add_item(pg)
        pages.append(pg)

    book.toc = [epub.Link("p0001.xhtml", title, "start")]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = pages
    return _write(book)


def _write(book):
    import io
    buf = io.BytesIO()
    epub.write_epub(buf, book)
    return buf.getvalue()
