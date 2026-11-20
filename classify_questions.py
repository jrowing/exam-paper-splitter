#!/usr/bin/env python3
"""
classify_questions.py - fill in the empty columns of bank/questions.db using Claude.

For every question not yet classified it sends the question image (plus the
extracted text) and gets back:
    primary_topic, secondary_topic, text_latex (question transcribed with LaTeX),
    has_equations, command_words, figure_types, figure_description, confidence

Setup
    pip install anthropic pillow
    export ANTHROPIC_API_KEY="sk-ant-..."     # an API key from console.anthropic.com
                                              # (API use is billed separately from a Claude.ai plan)

Use
    python classify_questions.py --bank bank --limit 10     # try 10 first and check them
    python classify_questions.py --bank bank                # then everything left
    python classify_questions.py --bank bank --paper 4      # only Paper 4 questions
    python classify_questions.py --bank bank --redo         # reclassify already-done questions

The first run writes bank/topics.txt (one topic per line) and stops so you can edit the list
to match your syllabus. Re-run to carry on. It is safe to stop and restart: finished
questions are skipped.
"""
import argparse
import base64
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import anthropic
from PIL import Image

MODEL = "claude-sonnet-5-5"

DEFAULT_TOPICS = """\
Physical quantities and units
Kinematics
Dynamics
Forces, density and pressure
Work, energy and power
Deformation of solids
Motion in a circle
Gravitational fields
Oscillations
Waves
Superposition
Communication
Electric current and D.C. circuits
Electric fields
Capacitance
Electronic sensing
Magnetic fields
Electromagnetic induction
Alternating currents
Temperature and thermal properties
Ideal gases
Thermodynamics
Quantum physics
Particle and nuclear physics
Medical physics
Astronomy and cosmology
Practical skills
"""

FIGURE_TYPES = ["circuit diagram", "graph", "ray diagram", "apparatus diagram", "force diagram",
                "field diagram", "waveform", "table", "photograph", "other"]


def build_system_prompt(topics):
    return (
        "You are cataloguing Cambridge International AS & A Level Physics (9702) exam questions "
        "for a teacher's question bank.\n\n"
        "You receive ONE question as consecutive image slices (top to bottom; a slice may cut through "
        "a line, so read them as one continuous page) plus text extracted from the PDF. The extracted "
        "text can be garbled, especially equations: the image is authoritative.\n\n"
        "Reply with ONLY a JSON object (no commentary, no code fences) with these keys:\n"
        '  "text_latex": the complete question transcribed in Markdown. Write all maths as LaTeX in $...$. '
        "Keep part labels such as (a), (i) and mark allocations such as [2]. Where a figure appears write "
        "[Figure: short description]. Omit answer lines, dotted lines and page furniture "
        "(copyright lines, 'Turn over').\n"
        '  "primary_topic": exactly one name from the topic list below.\n'
        '  "secondary_topic": exactly one name from the list if the question genuinely draws on a second '
        "topic, otherwise null.\n"
        '  "has_equations": true if the question contains equations, formulae or symbolic expressions.\n'
        '  "command_words": lowercase list of the Cambridge command words used, e.g. ["calculate","explain"].\n'
        '  "figure_types": list drawn from ' + json.dumps(FIGURE_TYPES) + "; [] if there are no figures.\n"
        '  "figure_description": one or two sentences describing each figure so it can be found by a text '
        'search (axes and quantities for graphs, components for circuits); "" if there are none.\n'
        '  "confidence": a number from 0 to 1 for how sure you are of the topic assignment.\n\n'
        "For practical, planning, analysis or evaluation questions, give the physics topic being "
        'investigated as primary_topic and use "Practical skills" as secondary_topic.\n\n'
        "Topic list (use these exact names):\n" + "\n".join(f"- {t}" for t in topics)
    )


def image_blocks(path, max_w=1400, slice_h=1400):
    """Resize and cut a tall question image into slices so text stays legible."""
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    blocks = []
    for top in range(0, im.height, slice_h):
        part = im.crop((0, top, im.width, min(top + slice_h, im.height)))
        if part.height < 40:
            continue
        buf = io.BytesIO()
        part.save(buf, "PNG")
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(buf.getvalue()).decode()}})
    return blocks


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def match_topic(value, topics):
    if not value:
        return None
    lookup = {t.lower(): t for t in topics}
    return lookup.get(str(value).strip().lower())


def classify(client, system, bank, row, topics):
    content = image_blocks(bank / row["image_path"])
    meta = f"Syllabus {row['syllabus']}, paper {row['paper']}, {row['session']} {row['year']}, question {row['question_number']}."
    content.append({"type": "text", "text": f"{meta}\n\nExtracted text (may be garbled):\n{row['text'] or ''}"})
    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": content}])
    data = parse_json("".join(b.text for b in resp.content if b.type == "text"))
    return data, resp.usage.input_tokens, resp.usage.output_tokens


def ensure_columns(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(questions)")}
    if "topic_confidence" not in have:
        con.execute("ALTER TABLE questions ADD COLUMN topic_confidence REAL")


def save(con, qid, data, topics, review_below):
    primary = match_topic(data.get("primary_topic"), topics)
    secondary = match_topic(data.get("secondary_topic"), topics)
    conf = float(data.get("confidence") or 0)
    figs = [f for f in (data.get("figure_types") or []) if f]
    needs_review = primary is None or conf < review_below
    con.execute(
        """UPDATE questions SET
             primary_topic=?, secondary_topic=?, text_latex=?, has_equations=?,
             command_words=?, figure_types=?, figure_description=?,
             has_diagram = MAX(COALESCE(has_diagram,0), ?),
             topic_confidence=?, review_status=?
           WHERE id=?""",
        (primary or f"UNMATCHED: {data.get('primary_topic')}", secondary, data.get("text_latex"),
         int(bool(data.get("has_equations"))), ", ".join(data.get("command_words") or []),
         "; ".join(figs), data.get("figure_description") or "", int(bool(figs)),
         conf, "needs_review" if needs_review else "auto", qid))
    con.commit()
    return primary, conf, needs_review


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bank", type=Path, default=Path("bank"))
    ap.add_argument("--limit", type=int, help="only do this many questions (good for a first test)")
    ap.add_argument("--paper", help="only questions from this paper number, e.g. 4")
    ap.add_argument("--redo", action="store_true", help="reclassify questions that already have a topic")
    ap.add_argument("--review-below", type=float, default=0.7, help="confidence under which a question is flagged needs_review")
    a = ap.parse_args()

    topics_file = a.bank / "topics.txt"
    if not topics_file.exists():
        topics_file.write_text(DEFAULT_TOPICS)
        sys.exit(f"Wrote a starting topic list to {topics_file}.\n"
                 "Edit it to match your syllabus (one topic per line), then run this again.")
    topics = [t.strip() for t in topics_file.read_text().splitlines() if t.strip()]

    con = sqlite3.connect(a.bank / "questions.db")
    con.row_factory = sqlite3.Row
    ensure_columns(con)

    sql = "SELECT * FROM questions WHERE 1=1"
    params = []
    if not a.redo:
        sql += " AND (primary_topic IS NULL OR primary_topic = '')"
    if a.paper:
        sql += " AND paper = ?"
        params.append(a.paper)
    sql += " ORDER BY id"
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    rows = con.execute(sql, params).fetchall()
    print(f"{len(rows)} questions to classify with {MODEL}")

    client = anthropic.Anthropic(max_retries=5)  # retries rate limits and server errors itself
    system = build_system_prompt(topics)
    tin = tout = failed = flagged = 0
    for i, row in enumerate(rows, 1):
        try:
            data, ti, to = classify(client, system, a.bank, row, topics)
            primary, conf, review = save(con, row["id"], data, topics, a.review_below)
            tin, tout, flagged = tin + ti, tout + to, flagged + review
            print(f"[{i}/{len(rows)}] {row['id']} -> {primary} ({conf:.2f}){'  <- review' if review else ''}")
        except KeyboardInterrupt:
            print("\nStopped. Re-run to carry on from here.")
            break
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(rows)}] {row['id']} FAILED: {e}")

    print(f"\nDone. {flagged} flagged needs_review, {failed} failed. "
          f"Tokens used: {tin:,} in, {tout:,} out.")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
