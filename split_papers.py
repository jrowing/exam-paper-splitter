#!/usr/bin/env python3
"""
split_papers.py - split digital exam-paper PDFs into one record per question.

For every question it saves:
  * a full-page-width image crop (diagrams, graphs and equations exactly as printed)
  * separate crops of any diagrams/graphs found inside the question
  * the extracted text, the marks, and the paper metadata
into a folder + SQLite database. Topic / LaTeX / figure-description columns are
created empty, ready for the Claude classification step.

Usage:
    pip install pymupdf pillow
    python split_papers.py papers/ --out bank/            # a folder of PDFs
    python split_papers.py 9702_s23_qp_42.pdf --out bank/ --debug

File names are expected to look like Cambridge's:  9702_s23_qp_42.pdf
(syllabus _ session+year _ qp _ paper+variant). Other names still work; the
metadata columns are just left blank.
"""
import argparse
import io
import re
import sqlite3
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

ZOOM = 3.0          # 3.0 = 216 dpi. Raise for sharper subscripts and gridlines.
PAD = 6             # points of padding around crops
SESSIONS = {"m": "Feb/March", "s": "May/June", "w": "Oct/Nov"}
FNAME = re.compile(
    r"(?P<syl>\d{4})_(?P<ses>[msw])(?P<yr>\d{2})_qp_(?P<paper>\d)(?P<var>\d)", re.I
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    syllabus TEXT, session TEXT, year INTEGER, paper TEXT, variant TEXT,
    source_file TEXT,
    question_number INTEGER,
    marks INTEGER,
    text TEXT,
    text_latex TEXT,
    image_path TEXT,
    figure_paths TEXT,
    figure_types TEXT,
    figure_description TEXT,
    has_diagram INTEGER,
    has_equations INTEGER,
    primary_topic TEXT,
    secondary_topic TEXT,
    command_words TEXT,
    mark_scheme_path TEXT,
    review_status TEXT DEFAULT 'auto'
);
"""


# ---------------------------------------------------------------- detection
def find_question_starts(doc, a):
    """Find question numbers 1, 2, 3... in the left margin, in sequence."""
    starts, expected = [], 1
    for pno in range(a.first_page, len(doc)):
        page = doc[pno]
        h = page.rect.height
        words = sorted(page.get_text("words"), key=lambda w: (w[1], w[0]))
        for x0, y0, x1, y1, txt, *_ in words:
            if x0 > a.left_max or y0 < a.top or y1 > h - a.bottom:
                continue
            if txt == str(expected):
                starts.append((pno, y0, expected))
                expected += 1
    return starts


def is_blank(page, a):
    text = page.get_text()
    if "BLANK PAGE" in text:
        return True
    area = fitz.Rect(0, a.top, page.rect.width, page.rect.height - a.bottom)
    return len(page.get_text("words", clip=area)) < 5


def content_bottom(page, rect):
    """Lowest point of real content inside rect (text or drawn lines)."""
    ys = [w[3] for w in page.get_text("words", clip=rect)]
    for d in page.get_drawings():
        r = d["rect"]
        if r.intersects(rect):
            ys.append(min(r.y1, rect.y1))
    return max(ys) if ys else None


def build_segments(doc, starts, a):
    """question number -> list of (page_no, Rect); handles page-spanning questions."""
    out = {}
    for i, (pno, y, q) in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        last_page = nxt[0] if nxt else len(doc) - 1
        segs = []
        for p in range(pno, last_page + 1):
            page = doc[p]
            r = page.rect
            if nxt is None and p > pno and "Permission to reproduce" in page.get_text():
                break
            if p != pno and not (nxt and p == nxt[0]) and is_blank(page, a):
                continue
            top = max(y - PAD, a.top) if p == pno else a.top
            bot = nxt[1] - PAD if (nxt and p == nxt[0]) else r.height - a.bottom
            if bot - top < 15:
                continue
            rect = fitz.Rect(a.side, top, r.width - a.side, bot)
            cb = content_bottom(page, rect)
            if cb is None:
                continue
            rect.y1 = min(rect.y1, cb + 8)
            segs.append((p, rect))
        out[q] = segs
    return out


# ------------------------------------------------------------------ output
def render(page, rect):
    if rect.is_empty or rect.width < 2 or rect.height < 2:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=rect, alpha=False)
    if pix.width < 2 or pix.height < 2:
        return None
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def stitch(images):
    images = [i for i in images if i is not None]
    if not images:
        return None
    w = max(i.width for i in images)
    canvas = Image.new("RGB", (w, sum(i.height for i in images)), "white")
    y = 0
    for im in images:
        canvas.paste(im, (0, y))
        y += im.height
    return canvas


def find_figures(page, rect):
    """Heuristic: clusters of vector drawings plus embedded images."""
    boxes = []
    try:
        boxes += list(page.cluster_drawings(clip=rect))
    except Exception:
        pass
    for info in page.get_image_info():
        boxes.append(fitz.Rect(info["bbox"]))
    boxes = [b & rect for b in boxes]
    boxes = [b for b in boxes if not b.is_empty and b.width >= 50 and b.height >= 40]
    boxes.sort(key=lambda b: -b.get_area())
    kept = []
    for b in boxes:  # drop boxes mostly inside an already-kept bigger one
        if not any((b & k).get_area() > 0.8 * b.get_area() for k in kept):
            kept.append(b)
    out = []
    for b in sorted(kept, key=lambda b: b.y0):
        b = fitz.Rect(b.x0 - 12, b.y0 - 12, b.x1 + 12, b.y1 + 12) & rect  # keep axis labels
        if not b.is_empty and b.width >= 10 and b.height >= 10:
            out.append(b)
    return out


def get_marks(text):
    m = re.search(r"\[Total:\s*(\d+)\]", text)
    if m:
        return int(m.group(1))
    parts = re.findall(r"\[(\d{1,2})\]", text)
    return sum(int(p) for p in parts) if parts else None


def process_pdf(pdf, a, con):
    m = FNAME.search(pdf.name)
    meta = dict(syllabus=None, session=None, year=None, paper=None, variant=None)
    if m:
        meta = dict(
            syllabus=m["syl"], session=SESSIONS[m["ses"].lower()], year=2000 + int(m["yr"]),
            paper=m["paper"], variant=m["var"],
        )
    stem = pdf.stem.replace("_qp", "")
    doc = fitz.open(pdf)
    starts = find_question_starts(doc, a)
    if not starts:
        print(f"  ! no question numbers found in {pdf.name} (try --left-max / --first-page)")
        return
    segments = build_segments(doc, starts, a)

    for q, segs in segments.items():
        if not segs:
            continue
        qid = f"{stem}_q{q:02d}"
        img_rel = f"questions/{qid}.png"
        q_img = stitch([render(doc[p], r) for p, r in segs])
        if q_img is None:
            print(f"  ! Q{q}: nothing to render, skipped")
            continue
        q_img.save(a.out / img_rel, optimize=True)

        text = "\n".join(doc[p].get_text("text", clip=r).strip() for p, r in segs)
        fig_paths, n = [], 0
        for p, r in segs:
            for fb in find_figures(doc[p], r):
                try:
                    fig = render(doc[p], fb)
                    if fig is None:
                        continue
                    rel = f"figures/{qid}_fig{n + 1}.png"
                    fig.save(a.out / rel, optimize=True)
                    n += 1
                    fig_paths.append(rel)
                except Exception as e:  # one bad figure shouldn't stop the run
                    print(f"  ! Q{q}: figure skipped ({e})")

        con.execute(
            """INSERT OR REPLACE INTO questions
               (id, syllabus, session, year, paper, variant, source_file, question_number,
                marks, text, image_path, figure_paths, has_diagram)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (qid, meta["syllabus"], meta["session"], meta["year"], meta["paper"],
             meta["variant"], pdf.name, q, get_marks(text), text, img_rel,
             ";".join(fig_paths), int(bool(fig_paths))),
        )
    con.commit()
    print(f"  {pdf.name}: {len(starts)} questions")

    if a.debug:  # annotated pages so you can check the boundaries by eye
        dbg = fitz.open(pdf)
        for q, segs in segments.items():
            for p, r in segs:
                pg = dbg[p]
                pg.draw_rect(r, color=(1, 0, 0), width=1.2)
                pg.insert_text((r.x0 + 2, r.y0 + 10), f"Q{q}", color=(1, 0, 0), fontsize=9)
        for p in sorted({p for segs in segments.values() for p, _ in segs}):
            dbg[p].get_pixmap(matrix=fitz.Matrix(1.2, 1.2)).save(a.out / "debug" / f"{stem}_p{p+1}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="a PDF or a folder of PDFs")
    ap.add_argument("--out", type=Path, default=Path("bank"))
    ap.add_argument("--first-page", type=int, default=1, help="0-based page to start looking for Q1 (default 1 = skip cover)")
    ap.add_argument("--left-max", type=float, default=75, help="question numbers must start left of this x (pt)")
    ap.add_argument("--top", type=float, default=40, help="ignore this much of the page top (header)")
    ap.add_argument("--bottom", type=float, default=50, help="ignore this much of the page bottom (footer)")
    ap.add_argument("--side", type=float, default=20, help="left/right crop margin (pt)")
    ap.add_argument("--debug", action="store_true", help="write annotated pages to OUT/debug")
    a = ap.parse_args()

    for sub in ("questions", "figures", "debug"):
        (a.out / sub).mkdir(parents=True, exist_ok=True)
    pdfs = sorted(a.input.glob("*.pdf")) if a.input.is_dir() else [a.input]
    con = sqlite3.connect(a.out / "questions.db")
    con.executescript(SCHEMA)
    for pdf in pdfs:
        process_pdf(pdf, a, con)
    con.close()


if __name__ == "__main__":
    sys.exit(main())
