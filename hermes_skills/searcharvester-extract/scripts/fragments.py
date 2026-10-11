#!/usr/bin/env python3
"""Query-aware fragment selection over a saved markdown extract.

Two layers:

- chunk_markdown(text): structure-preserving split. Units are headings (ATX
  and setext), paragraphs, fenced code blocks, tables, lists and blockquotes.
  Units of one section pack into chunks of about target_chars; fences and
  tables are never split mid-block, other oversize units split at line
  boundaries, and a single line longer than ~2x target is hard-split at char
  boundaries (real extracts contain e.g. a 296 KB embedded-SVG line).
- rank_chunks(chunks, query): stdlib BM25 (k1=1.5, b=0.75, BM25+ idf so
  scores stay non-negative). Cyrillic works because the tokenizer is unicode
  word characters, lowercased.

Each chunk carries its heading path, a 1-based inclusive line range and
neighbor chunk ids, so a reader can escalate with `sed -n 'a,bp' file` or
open the full page. Fragments are a read mode, not a filter: the file stays.

Standalone re-rank of an already-saved extract:

    python3 fragments.py --file ./extracts/abc123.md --query "TLS fragmentation"
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_ATX_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SETEXT_RE = re.compile(r"^(=+|-{3,})\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_TABLE_RE = re.compile(r"^\s*\|")
_QUOTE_RE = re.compile(r"^\s*>")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

DEFAULT_TARGET_CHARS = 2400          # ~600 tokens at chars/4
TAIL_MERGE_CHARS = 600               # trailing chunk smaller than this merges back
ATOMIC_KINDS = ("code", "table")     # never split mid-block


@dataclass
class Unit:
    kind: str        # heading | text | code | table | list | quote
    start: int       # 1-based first line
    end: int         # 1-based inclusive last line
    lines: list[str]
    level: int = 0   # heading level, 0 for non-headings
    title: str = ""

    @property
    def chars(self) -> int:
        return sum(len(l) + 1 for l in self.lines)


@dataclass
class Chunk:
    idx: int
    heading_path: list[str]
    kind: str
    line_start: int
    line_end: int
    oversize: bool
    text: str


def _is_special(line: str) -> bool:
    return bool(
        _FENCE_RE.match(line)
        or _ATX_RE.match(line)
        or _TABLE_RE.match(line)
        or _LIST_RE.match(line)
        or _QUOTE_RE.match(line)
    )


def _units(lines: list[str]) -> list[Unit]:
    """Split raw markdown lines into typed units; blanks are separators."""
    units: list[Unit] = []
    i, n = 0, len(lines)
    # YAML frontmatter: a leading --- ... --- block is one inert text unit,
    # not a setext heading (which is how a bare --- would otherwise parse).
    if n and lines[0].strip() == "---":
        j = 1
        while j < n and lines[j].strip() not in ("---", "..."):
            j += 1
        if j < n:
            units.append(Unit("text", 1, j + 1, lines[:j + 1]))
            i = j + 1
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)[:3]
            j = i + 1
            while j < n and not lines[j].lstrip().startswith(fence):
                j += 1
            j = min(j + 1, n)          # include the closing fence when present
            units.append(Unit("code", i + 1, j, lines[i:j]))
            i = j
            continue
        m = _ATX_RE.match(line)
        if m:
            units.append(Unit("heading", i + 1, i + 1, [line],
                              level=len(m.group(1)), title=m.group(2)))
            i += 1
            continue
        if _TABLE_RE.match(line):
            j = i
            while j < n and _TABLE_RE.match(lines[j]):
                j += 1
            units.append(Unit("table", i + 1, j, lines[i:j]))
            i = j
            continue
        if _LIST_RE.match(line):
            j = i
            while j < n and (_LIST_RE.match(lines[j])
                             or (lines[j].strip() and lines[j][:1] in (" ", "\t"))):
                j += 1
            units.append(Unit("list", i + 1, j, lines[i:j]))
            i = j
            continue
        if _QUOTE_RE.match(line):
            j = i
            while j < n and _QUOTE_RE.match(lines[j]):
                j += 1
            units.append(Unit("quote", i + 1, j, lines[i:j]))
            i = j
            continue
        # Paragraph: non-blank lines until a special opener. A setext
        # underline right after the text turns the whole run into a heading
        # (CommonMark); a bare --- with no preceding text line is inert.
        j = i
        took_setext = False
        while j < n and lines[j].strip() and not _is_special(lines[j]):
            if _SETEXT_RE.match(lines[j]) and j > i:
                lvl = 1 if lines[j].startswith("=") else 2
                title = " ".join(l.strip() for l in lines[i:j])
                units.append(Unit("heading", i + 1, j + 1, lines[i:j + 1],
                                  level=lvl, title=title))
                took_setext = True
                break
            j += 1
        if took_setext:
            i = j + 1
        else:
            units.append(Unit("text", i + 1, j, lines[i:j]))
            i = j
    return units


def _split_oversize(unit: Unit, target: int) -> list[Unit]:
    """Split a non-atomic oversize unit at line boundaries; a single line
    longer than ~2x target hard-splits at char boundaries."""
    pieces: list[Unit] = []
    cur: list[str] = []
    cur_start = unit.start
    cur_chars = 0
    lineno = unit.start
    for line in unit.lines:
        if len(line) + 1 > target * 2:
            if cur:
                pieces.append(Unit(unit.kind, cur_start, lineno - 1, cur))
                cur, cur_chars = [], 0
            for k in range(0, len(line), target):
                seg = line[k:k + target]
                # one logical line becomes several pieces sharing the line no.
                pieces.append(Unit(unit.kind, lineno, lineno, [seg]))
            lineno += 1
            cur_start = lineno
            continue
        if cur and cur_chars + len(line) + 1 > target:
            pieces.append(Unit(unit.kind, cur_start, lineno - 1, cur))
            cur, cur_chars = [], 0
            cur_start = lineno
        cur.append(line)
        cur_chars += len(line) + 1
        lineno += 1
    if cur:
        pieces.append(Unit(unit.kind, cur_start, lineno - 1, cur))
    return pieces


def _merge_units(units: list[Unit]) -> tuple[str, int, int, str]:
    kinds = {u.kind for u in units if u.kind != "heading"}
    kind = kinds.pop() if len(kinds) == 1 else ("mixed" if kinds else units[0].kind)
    return kind, units[0].start, units[-1].end, "\n".join(
        l for u in units for l in u.lines)


def chunk_markdown(text: str, target_chars: int = DEFAULT_TARGET_CHARS) -> list[Chunk]:
    target_chars = max(1, target_chars)
    lines = text.split("\n")
    # raw rows: (heading_path, oversize, kind, line_start, line_end, text)
    raw: list[tuple[list[str], bool, str, int, int, str]] = []
    cur: list[Unit] = []
    cur_chars = 0
    cur_path: list[str] = []
    stack: list[tuple[int, str]] = []

    def flush():
        nonlocal cur_chars
        if not cur:
            return
        kind, a, b, body = _merge_units(cur)
        raw.append((list(cur_path), len(body) > target_chars, kind, a, b, body))
        cur.clear()
        cur_chars = 0

    for u in _units(lines):
        # A single line longer than ~2x target is not a real heading/paragraph
        # (real case: 296 KB embedded SVG). Hard-split it regardless of kind,
        # except atomic code/table units which stay whole and get flagged.
        if u.chars > target_chars and u.kind in ATOMIC_KINDS:
            flush()
            kind, a, b, body = _merge_units([u])
            raw.append((list(cur_path), True, kind, a, b, body))
            continue
        if u.chars > 2 * target_chars:
            flush()
            for piece in _split_oversize(u, target_chars):
                kind, a, b, body = _merge_units([piece])
                raw.append((list(cur_path), piece.chars > target_chars,
                            kind, a, b, body))
            continue
        if u.kind == "heading":
            flush()
            while stack and stack[-1][0] >= u.level:
                stack.pop()
            stack.append((u.level, u.title))
            cur_path = [t for _, t in stack]
            cur.append(u)
            cur_chars = u.chars
            continue
        if u.chars > target_chars:
            # non-atomic unit, 1x-2x target: split at line boundaries
            flush()
            for piece in _split_oversize(u, target_chars):
                kind, a, b, body = _merge_units([piece])
                raw.append((list(cur_path), piece.chars > target_chars,
                            kind, a, b, body))
            continue
        if cur and cur_chars + u.chars > target_chars:
            flush()
        cur.append(u)
        cur_chars += u.chars
    flush()

    # Tail merge: a small chunk joins a same-section neighbor when the result
    # stays near target (no tiny tails, but never re-merge into a huge one).
    merged: list[tuple[list[str], bool, str, int, int, str]] = []
    for path, oversize, kind, a, b, body in raw:
        if (merged and merged[-1][0] == path and len(body) < TAIL_MERGE_CHARS
                and len(merged[-1][5]) + len(body) <= int(target_chars * 1.25)):
            p, ov, pk, pa, pb, pbody = merged[-1]
            merged[-1] = (p, ov or oversize,
                          pk if pk == kind else "mixed", pa, b, pbody + "\n" + body)
        else:
            merged.append((path, oversize, kind, a, b, body))

    return [Chunk(idx=i, heading_path=path, kind=kind, line_start=a,
                  line_end=b, oversize=oversize, text=body)
            for i, (path, oversize, kind, a, b, body) in enumerate(merged)]


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall(s.lower())


def rank_chunks(chunks: list[Chunk], query: str, top_k: int = 3,
                k1: float = 1.5, b: float = 0.75) -> list[tuple[Chunk, float]]:
    q = list(dict.fromkeys(_tokens(query)))
    if not q or not chunks:
        return []
    docs = [_tokens(" ".join(c.heading_path) + " " + c.text) for c in chunks]
    n = len(docs)
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    avg = sum(len(d) for d in docs) / max(n, 1) or 1.0
    idf = {t: math.log(1.0 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
           for t in q}
    scored: list[tuple[Chunk, float]] = []
    for c, d in zip(chunks, docs):
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d) or 1
        s = sum(idf[t] * tf.get(t, 0) * (k1 + 1)
                / (tf.get(t, 0) + k1 * (1 - b + b * dl / avg)) for t in q)
        if s > 0:
            scored.append((c, s))
    scored.sort(key=lambda x: (-x[1], x[0].idx))
    return scored[:top_k]


def fragment_json(chunk: Chunk, score: float, rank: int,
                  chunks: list[Chunk], emit_cap: int) -> dict:
    text = chunk.text
    if len(text) > emit_cap:
        text = (text[:emit_cap]
                + f"\n[... +{len(chunk.text) - emit_cap} chars, "
                  f"lines {chunk.line_start}-{chunk.line_end}]")
    total = len(chunks)
    # Neighbors carry their own 1-based line ranges so the agent can sed
    # them directly; bare ids alone are not resolvable to content.
    neighbors = [{"chunk": i, "lines": [chunks[i].line_start,
                                      chunks[i].line_end]}
                 for i in (chunk.idx - 1, chunk.idx + 1) if 0 <= i < total]
    return {
        "rank": rank,
        "score": round(score, 3),
        "chunk": chunk.idx,
        "heading_path": chunk.heading_path,
        "lines": [chunk.line_start, chunk.line_end],
        "chars": len(chunk.text),
        "oversize": chunk.oversize,
        "text": text,
        "neighbors": neighbors,
    }


def fragments_payload(markdown: str, path: str, query: str,
                      top_k: int = 3,
                      target_chars: int = DEFAULT_TARGET_CHARS) -> dict:
    """The fragments block embedded into extract.py output and printed by the
    standalone CLI. Escalation currency is the 1-based line range. Note:
    hard-split pieces of one overlong source line share that line number, so
    `lines` ranges may overlap between chunks -- escalation reads a superset,
    which is the safe direction."""
    target_chars = max(1, target_chars)
    chunks = chunk_markdown(markdown, target_chars=target_chars)
    ranked = rank_chunks(chunks, query, top_k=max(1, top_k))
    return {
        "path": path,
        "fragments": [fragment_json(c, s, i + 1, chunks, target_chars)
                      for i, (c, s) in enumerate(ranked)],
        "fragments_total": len(chunks),
        "zero_match": not ranked,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Rank the fragments of a saved markdown extract against a "
                    "query (BM25 over structure-aware chunks).")
    p.add_argument("--file", required=True, help="path to a saved .md extract")
    p.add_argument("--query", required=True, help="sub-question to rank against")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--target-chars", type=int, default=DEFAULT_TARGET_CHARS)
    args = p.parse_args()
    try:
        text = open(args.file, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    payload = fragments_payload(text, args.file, args.query,
                                top_k=args.top_k,
                                target_chars=args.target_chars)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
