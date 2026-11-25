"""Cleaned Markdown -> accessible Word (.docx): real heading styles, list
styles, table header rows, document title/author/language set."""
import io

import markdown
from bs4 import BeautifulSoup, NavigableString
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


def _add_runs(par, node, bold=False, italic=False):
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                run = par.add_run(text)
                run.bold, run.italic = bold or None, italic or None
                # Word uses separate switches for Telugu (complex script) text
                rpr = run._r.get_or_add_rPr()
                for on, tag in ((bold, "w:bCs"), (italic, "w:iCs")):
                    if on:
                        rpr.append(OxmlElement(tag))
        elif child.name in ("strong", "b"):
            _add_runs(par, child, True, italic)
        elif child.name in ("em", "i"):
            _add_runs(par, child, bold, True)
        elif child.name == "br":
            par.add_run().add_break()
        else:
            _add_runs(par, child, bold, italic)


WORD_LANG = {"te": "te-IN", "hi": "hi-IN", "en": "en-IN"}
INDIC = {"te", "hi"}  # scripts Word treats as "complex script"


def _set_language(doc, lang):
    """Tag text language (screen readers, spell-check) and set a Telugu font."""
    code = WORD_LANG.get(lang, lang)
    names = ["Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4",
             "Quote", "List Bullet", "List Number"]
    for name in names:
        try:
            rpr = doc.styles[name].element.get_or_add_rPr()
        except KeyError:
            continue
        for old in rpr.findall(qn("w:lang")):
            rpr.remove(old)
        el = OxmlElement("w:lang")
        el.set(qn("w:val"), "en-IN" if lang in INDIC else code)  # Latin-script text
        el.set(qn("w:bidi"), code)  # Word files Telugu under "complex script"
        rpr.append(el)
        if lang in INDIC:
            fonts = rpr.find(qn("w:rFonts"))
            if fonts is None:
                fonts = OxmlElement("w:rFonts")
                rpr.insert(0, fonts)
            for attr in ("w:cstheme",):
                fonts.attrib.pop(qn(attr), None)
            fonts.set(qn("w:cs"), "Nirmala UI")  # Windows font covering Telugu & Devanagari


def _table(doc, tbl):
    rows = tbl.find_all("tr")
    if not rows:
        return
    ncols = max(len(r.find_all(["td", "th"])) for r in rows)
    t = doc.add_table(rows=0, cols=ncols)
    t.style = "Table Grid"
    for ri, r in enumerate(rows):
        cells = t.add_row().cells
        for ci, c in enumerate(r.find_all(["td", "th"])):
            p = cells[ci].paragraphs[0]
            _add_runs(p, c, bold=(c.name == "th"))
        if ri == 0 and r.find("th"):  # mark header row (repeats + read as header)
            trpr = t.rows[0]._tr.get_or_add_trPr()
            hdr = OxmlElement("w:tblHeader")
            hdr.set(qn("w:val"), "true")
            trpr.append(hdr)


def build_docx(md_text, title, author, lang="en"):
    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = author or ""
    doc.core_properties.language = lang
    _set_language(doc, lang)
    doc.styles["Normal"].font.size = Pt(12)

    doc.add_heading(title, level=0)
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists"])
    soup = BeautifulSoup(html, "html.parser")

    for el in soup.children:
        if isinstance(el, NavigableString):
            continue
        name = el.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            h = doc.add_heading(level=int(name[1]))
            _add_runs(h, el)
        elif name == "p":
            _add_runs(doc.add_paragraph(), el)
        elif name in ("ul", "ol"):
            style = "List Bullet" if name == "ul" else "List Number"
            for li in el.find_all("li", recursive=False):
                _add_runs(doc.add_paragraph(style=style), li)
        elif name == "blockquote":
            for p in el.find_all("p") or [el]:
                _add_runs(doc.add_paragraph(style="Quote"), p)
        elif name == "table":
            _table(doc, el)
        elif name == "hr":
            doc.add_page_break()
        else:
            _add_runs(doc.add_paragraph(), el)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
