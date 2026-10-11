"""Tests for the extract skill's fragment mode (fragments.py + extract.py
--query): structure-aware chunking, BM25 ranking and the escalation ladder.

The module under test lives inside the skill dir so it travels with the skill;
we reach it through the same SKILLS resolution test_invariants uses.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("REPO_ROOT", HERE.parent.parent))
SKILLS = REPO / "hermes_skills"
if not SKILLS.exists():
    SKILLS = Path("/opt/searcharvester-skills")

SCRIPTS = SKILLS / "searcharvester-extract" / "scripts"
if SCRIPTS.exists():
    sys.path.insert(0, str(SCRIPTS))
    import fragments  # noqa: E402
else:
    fragments = None

needs_skills = pytest.mark.skipif(fragments is None, reason="skills not found")

DOC = """# Title

Intro paragraph about cats and dogs.

## Setup

Install the package with pip install thing.
More setup detail here.

## API

The api_token parameter controls authentication.
Send it in the Authorization header.

```python
def call():
    return 42
```

## Results

| col | val |
|-----|-----|
| a   | 1   |
| b   | 2   |

Closing words.
"""


@needs_skills
def test_chunks_tile_the_document():
    chunks = fragments.chunk_markdown(DOC)
    covered = set()
    for c in chunks:
        for ln in range(c.line_start, c.line_end + 1):
            assert ln not in covered, f"line {ln} in two chunks"
            covered.add(ln)
    text_lines = {i + 1 for i, l in enumerate(DOC.split("\n")) if l.strip()}
    assert text_lines <= covered


@needs_skills
def test_fence_and_table_never_split_mid_block():
    fence = "```\n" + "\n".join(f"line {i} " + "x" * 60 for i in range(80)) + "\n```"
    doc = "# T\n\n" + fence + "\n\n| a | b |\n|---|---|\n" + "".join(
        f"| {i} | {'y' * 80} |\n" for i in range(60))
    chunks = fragments.chunk_markdown(doc, target_chars=1000)
    code = [c for c in chunks if c.kind == "code"]
    table = [c for c in chunks if c.kind == "table"]
    assert len(code) == 1 and len(table) == 1
    assert code[0].oversize and table[0].oversize
    assert "```" in code[0].text and code[0].text.count("```") == 2


@needs_skills
def test_heading_path_stack_and_shallower_pop():
    doc = "# A\n\nx\n\n## B\n\ny\n\n### C\n\nz\n\n## D\n\nw\n"
    chunks = fragments.chunk_markdown(doc)
    paths = {tuple(c.heading_path) for c in chunks}
    assert ("A", "B", "C") in paths
    assert ("A", "D") in paths            # ## pops ### C off the stack
    assert not any(p[:1] != ("A",) for p in paths)


@needs_skills
def test_setext_heading_is_a_heading_unit():
    doc = "Some Title\n==========\n\nbody text\n\nOther\n-----\n\nmore\n"
    chunks = fragments.chunk_markdown(doc)
    paths = [c.heading_path for c in chunks]
    assert ["Some Title"] in paths
    assert ["Some Title", "Other"] in paths


@needs_skills
def test_single_line_longer_than_2x_target_hard_splits():
    doc = "# T\n\nintro\n\n" + ("x" * 9000) + "\n\noutro\n"
    chunks = fragments.chunk_markdown(doc, target_chars=2000)
    assert all(len(c.text) <= 2200 for c in chunks)
    assert sum(len(c.text) for c in chunks) >= 9000


@needs_skills
def test_small_tail_merges_into_previous_chunk():
    doc = "## S\n\n" + ("filler paragraph. " * 120) + "\n\ntrailing tiny bit.\n"
    chunks = fragments.chunk_markdown(doc, target_chars=2400)
    assert not any(len(c.text) < 200 and c.heading_path == ["S"]
                   for c in chunks)


@needs_skills
def test_bm25_ranks_relevant_chunk_first_en_and_ru():
    chunks = fragments.chunk_markdown(DOC)
    top = fragments.rank_chunks(chunks, "api_token authentication header", top_k=1)
    assert top and top[0][0].heading_path[-1] == "API"

    ru = "# Отчёт\n\nВводный текст про погоду и кошек.\n\n## Секция\n\nТСПУ замораживает соединение после 28 килобайт переданных данных. Повторные попытки дают тот же результат.\n\n## Прочее\n\nСовершенно посторонний текст про рецепты борща.\n"
    chunks = fragments.chunk_markdown(ru)
    top = fragments.rank_chunks(chunks, "обрыв соединения после килобайт", top_k=1)
    assert top and top[0][0].heading_path[-1] == "Секция"


@needs_skills
def test_zero_overlap_returns_empty_not_crash():
    chunks = fragments.chunk_markdown(DOC)
    assert fragments.rank_chunks(chunks, "zzz qqq nonexistent", top_k=3) == []
    payload = fragments.fragments_payload(DOC, "x.md", "zzz qqq nonexistent")
    assert payload["zero_match"] is True and payload["fragments"] == []


@needs_skills
def test_fragment_payload_shape_and_emission_cap():
    big = "# T\n\n" + "\n\n".join(f"para {i} " + "w " * 200 for i in range(30))
    payload = fragments.fragments_payload(big, "f.md", "para 7", top_k=3,
                                          target_chars=800)
    assert payload["fragments_total"] >= 3
    for f in payload["fragments"]:
        assert set(f) == {"rank", "score", "chunk", "heading_path", "lines",
                          "chars", "oversize", "text", "neighbors"}
        assert len(f["text"]) <= 900          # emit cap + marker
        for n in f["neighbors"]:
            # neighbors must be resolvable: own 1-based line range included
            assert 0 <= n["chunk"] < payload["fragments_total"]
            assert n["lines"][0] <= n["lines"][1]


def _load_extract():
    spec = importlib.util.spec_from_file_location(
        "extract_mod", SCRIPTS / "extract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@needs_skills
def test_extract_query_adds_fragments_and_same_output_without(
        tmp_path, monkeypatch, capsys):
    """--query must add fragments[] while the no-query JSON stays identical."""
    ext = _load_extract()
    full = "# Doc\n\n" + "alpha beta gamma\n\n" * 5 + "## Hit\n\nneedle lives here\n"
    fake_meta = {"id": "abc", "url": "https://x", "title": "t",
                 "total_chars": len(full), "pages_fetched": 1, "notice": None}
    monkeypatch.setattr(ext, "_fetch_all_pages",
                        lambda base, url: (fake_meta, full))
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(sys, "argv", ["extract.py", "--url", "https://x"])
    assert ext.main() == 0
    base_out = json.loads(capsys.readouterr().out)
    assert "fragments" not in base_out
    assert base_out["path"].endswith(".md")

    monkeypatch.setattr(sys, "argv",
                        ["extract.py", "--url", "https://x", "--query", "needle"])
    assert ext.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fragments"] and out["fragments"][0]["rank"] == 1
    assert out["fragments"][0]["lines"][0] <= out["fragments"][0]["lines"][1]
    for k in base_out:
        if k != "hint":
            assert out[k] == base_out[k]


@needs_skills
def test_real_saved_extract_chunks_cleanly():
    jobs = REPO / "jobs"
    if not jobs.exists():
        pytest.skip("no jobs/ corpus on this host")
    files = sorted(jobs.glob("*/extracts/*.md"), key=lambda p: p.stat().st_size)
    if not files:
        pytest.skip("no saved extracts")
    sample = files[len(files) // 2]          # median-size real extract
    text = sample.read_text(encoding="utf-8", errors="replace")
    chunks = fragments.chunk_markdown(text)
    assert chunks
    covered = set()
    for c in chunks:
        for ln in range(c.line_start, c.line_end + 1):
            assert ln not in covered
            covered.add(ln)
    content = {i + 1 for i, l in enumerate(text.split("\n")) if l.strip()}
    assert content <= covered


@needs_skills
def test_target_chars_zero_and_negative_do_not_crash():
    # Degenerate target clamps to 1; a dict comes back either way, no raise.
    for bad in (0, -50):
        payload = fragments.fragments_payload(DOC, "x.md", "cats", top_k=2,
                                              target_chars=bad)
        assert isinstance(payload, dict)
        assert "fragments" in payload and "zero_match" in payload


@needs_skills
def test_punctuation_only_doc_no_zero_division():
    # A page with zero word-tokens made BM25 avg doc length 0 -> crash.
    doc = "?!.,;:\n\n+++ === ???\n\n<></><>\n"
    payload = fragments.fragments_payload(doc, "x.md", "any query")
    assert payload["zero_match"] is True and payload["fragments"] == []


@needs_skills
def test_yaml_frontmatter_is_not_a_setext_heading():
    doc = "---\ntitle: hello\nauthor: x\n---\n\n## Real\n\nbody\n"
    chunks = fragments.chunk_markdown(doc)
    titles = {t for c in chunks for t in c.heading_path}
    assert "Real" in titles
    assert not any("title: hello" in t for t in titles)


@needs_skills
def test_giant_heading_line_hard_splits_not_atomic():
    doc = "# " + "h " * 4000 + "\n\nbody\n"
    chunks = fragments.chunk_markdown(doc, target_chars=2000)
    assert all(len(c.text) <= 2200 for c in chunks)


@needs_skills
def test_extract_query_sed_hint_is_valid_syntax(tmp_path, monkeypatch, capsys):
    """GNU sed ranges use ',' and multiple commands join with ';'."""
    import re
    ext = _load_extract()
    full = ("# D\n\n" + "filler words here\n\n" * 40 +
            "## Hit\n\nneedle one\n\n## Hit2\n\nneedle two\n")
    fake_meta = {"id": "abc", "url": "https://x", "title": "t",
                 "total_chars": len(full), "pages_fetched": 1, "notice": None}
    monkeypatch.setattr(ext, "_fetch_all_pages",
                        lambda base, url: (fake_meta, full))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv",
        ["extract.py", "--url", "https://x", "--query", "needle",
         "--fragments", "2"])
    assert ext.main() == 0
    out = json.loads(capsys.readouterr().out)
    # The base hint contains `sed -n '1,300p'` too; the fragment-escalation
    # command is the LAST sed mention in the hint.
    matches = re.findall(r"sed -n '([^']+)'", out["hint"])
    assert matches, out["hint"]
    frag_sed = matches[-1]
    assert re.fullmatch(r"(\d+,\d+p;?)+", frag_sed), frag_sed
