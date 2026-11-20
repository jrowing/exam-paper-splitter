# Exam paper splitter

Turn a folder of past exam papers (digital PDFs) into a searchable question bank: one record per question, with diagrams, graphs and equations kept exactly as printed, and each question tagged by topic.

Built for Cambridge International AS & A Level Physics (9702), but it works with any board's papers that number questions down the left margin.

## What you get

```
bank/
  questions.db          SQLite: one row per question
  questions/            full-width image of every question (page-spanning questions are stitched)
  figures/              separate crops of each diagram or graph
  topics.txt            your topic list
```

Each row holds the syllabus, session, year, paper and variant (read from the filename), question number, total marks, extracted text, image paths and, after classification, a LaTeX/Markdown transcription, primary and secondary topic, command words, figure types and a short figure description for searching.

## Step 1: split the papers

```bash
pip install -r requirements.txt
python split_papers.py papers/ --out bank/
python split_papers.py 9702_s23_qp_42.pdf --out bank/ --debug   # check the boundaries on one paper
```

Filenames in Cambridge's pattern (`9702_s23_qp_42.pdf`: syllabus, session + year, `qp`, paper + variant) fill in the metadata columns. Other names still work; those columns are just left blank.

`--debug` writes annotated page images to `bank/debug/` with each question's crop outlined in red. If questions are being missed, adjust `--left-max` (how far in from the left edge question numbers can sit) or `--first-page`.

## Step 2: classify by topic

```bash
mkdir -p bank && cp topics/9702-physics.txt bank/topics.txt   # or write your own, one topic per line
export ANTHROPIC_API_KEY=...
python classify_questions.py --bank bank --limit 10           # try a few first
python classify_questions.py --bank bank                      # then the rest
```

Each question image is sent to Claude, which returns the topic(s), a transcription with LaTeX maths, the command words used and a description of any figures. Questions where the model is unsure are marked `review_status = 'needs_review'` so you can check them by hand. The script is safe to stop and restart.

API use is billed per question. If you would rather not use an API key, you can do this step by hand, or in a Claude chat with the `questions/` images attached, writing the results into the database.

To adapt it to another subject, edit the topic list and the first paragraph of `build_system_prompt()` in `classify_questions.py`.

## Copyright: please read

Past papers, mark schemes and the questions in them are the exam board's copyright. This repository contains **code only**. Get papers from your board's teacher support site, keep the generated bank for use inside your school, and don't publish it. The `.gitignore` excludes PDFs, `bank/` and databases so a bank can't be committed by accident.

## Licence

MIT. See [LICENSE](LICENSE).
