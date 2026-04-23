---
name: searcharvester-deep-research
description: >
  Deep-research methodology with PARALLEL sub-agents. Given a research question,
  decompose it into 3–5 sub-questions, dispatch one batch of parallel sub-agents
  via delegate_task (each with searcharvester-search + searcharvester-extract
  skills), collect their structured findings, and synthesise a cited markdown
  report at /workspace/report.md. Use for "research", "deep research", "report
  with sources", comparisons with citations, anything requiring 15+ grounded
  web sources.
version: 2.1.0
author: Searcharvester
license: MIT
metadata:
  hermes:
    tags: [research, deep-research, delegate_task, parallel, subagents, citations]
    category: research
    related_skills:
      - searcharvester-search
      - searcharvester-extract
      - subagent-driven-development
      - writing-plans
      - plan
---

# Searcharvester Deep Research

Turn an open question into a cited markdown report grounded in 15+ web
sources, produced by **parallel sub-agents** and synthesised by you (the
lead). Save to `/workspace/report.md`.

## Role

You are the **lead researcher**. You **do not personally call
`searcharvester-search` or `searcharvester-extract`** — the sub-agents do.
Your job:
1. Decompose.
2. Dispatch one `delegate_task` **batch** covering all sub-questions at once.
3. Collect structured returns and synthesise.

## When to use

Use for any research task needing cited evidence:
- "Research / deep research / analyse X"
- "Compare A vs B with sources"
- "What's publicly known about [person/company/topic]"
- "Find contacts / profiles / papers for X"
- "What's the latest on X"

**Do not** use this skill for:
- Questions you can answer from your own knowledge.
- Coding tasks.

## Core principle

**Evidence before prose.** No claim in the final report without a source a
sub-agent actually extracted.

## Procedure

### Phase 1 — Decompose (lead)

Write a plan to `/workspace/plan.md` using the `terminal` tool:

```bash
cat > /workspace/plan.md << 'EOF'
## Intent
<one sentence on what the user wants>

## Sub-questions (3–5)
1. <concrete, factually researchable>
2. ...
5. ...

## Out of scope
- <things we won't cover>
EOF
```

Good sub-questions are:
- **Independent** (a sub-agent can answer it without knowing the others).
- **Concrete** (asks for facts, numbers, names, URLs — not "describe").
- **Focused** (each sub-agent aims at 4–6 extracts).

### Phase 2 — Dispatch ONE batch of sub-agents (lead)

Call `delegate_task` ONCE with a `tasks=[...]` array. This runs all
sub-agents in parallel (up to delegation.max_concurrent_children, currently
5). Each task gets the same template; only `goal` + embedded sub-question
differ:

```python
delegate_task(
    tasks=[
        {
            "goal": "Research sub-question 1: <sub-question text>",
            "context": SUBAGENT_CONTEXT_TEMPLATE.replace("<SUBQ>", "<sub-question 1>"),
            "toolsets": ["terminal"],
        },
        {
            "goal": "Research sub-question 2: <sub-question text>",
            "context": SUBAGENT_CONTEXT_TEMPLATE.replace("<SUBQ>", "<sub-question 2>"),
            "toolsets": ["terminal"],
        },
        # ... one per sub-question, 3–5 total
    ],
)
```

The sub-agent context template (bake verbatim into each task's `context`,
substituting `<SUBQ>` and `<USER_QUERY>`):

```
SUB-QUESTION YOU MUST ANSWER:
<SUBQ>

PARENT USER QUERY (for context only):
"<USER_QUERY>"

IMPORTANT: You only have the `terminal` toolset. The searcharvester
skills are NOT loaded into your agent — you must invoke them as shell
scripts through `terminal`. Use these EXACT commands:

  # Search the web (returns JSON of URLs + snippets)
  python3 /opt/data/skills/searcharvester-search/scripts/search.py \
    --query "<your query>" --max-results 5

  # Extract clean markdown from a URL
  python3 /opt/data/skills/searcharvester-extract/scripts/extract.py \
    --url "<url>" --size m

Do not try to call tools named "searcharvester-search" or
"searcharvester-extract" — those are not registered as tools in your
runtime. Go through `terminal` with the python3 commands above.

METHOD:
1. Run 2–4 `search.py` invocations with varied phrasings (different
   angles, plus English + the question's language when non-English).
2. From the combined JSON results, pick 4–6 authoritative URLs.
3. Call `extract.py --size m` on each picked URL.
4. If extract.py prints `{"error": "HTTP 422"|"HTTP 502"|...}`, skip
   that URL and pick another one.
5. Target: 4–6 successful extracts.

RETURN FORMAT — reply with a markdown block EXACTLY in this shape:
### Sub-question findings
- **Claim**: <one-sentence factual claim>
  **Quote**: "<verbatim quote from the source>"
  **URL**: <url>
- **Claim**: ...
(6–10 bullets total)

DO NOT write a full report.
DO NOT add sections outside the template.
DO NOT call delegate_task recursively.
```

### Phase 3 — Synthesise (lead)

Once `delegate_task` returns (it blocks until all sub-agents finish):

1. Collect each sub-agent's markdown block from the returned JSON.
2. Build a unified reference list — dedupe URLs, assign `[1]`, `[2]`, ...
   in order of first appearance.
3. Write `/workspace/report.md`:

```markdown
# <Title reflecting the actual question>

## TL;DR
<2–4 sentence standalone summary>

## <Sub-topic 1>
<Claims with inline [n] citations>

## <Sub-topic 2>
...

## Caveats and open questions
<Bullets — unanswered sub-questions, disagreements between sources>

## References
[1] <short label> — <URL>
[2] <short label> — <URL>
```

Hard rules:
- Every factual claim has an inline `[n]` citation.
- Same URL = same number.
- Use tables when comparing 3+ items.
- No marketing adjectives unless quoted.
- Numbers get units and dates when available.
- Match the user's language.

### Phase 4 — Verify and deliver

Before closing:
- [ ] `/workspace/report.md` exists and is > 500 bytes.
- [ ] References list has **≥ 15 unique URLs** (target 20–30).
- [ ] Every non-TL;DR, non-References section has at least one `[n]`.
- [ ] TL;DR is standalone (no citations, no "see below").

Then print as the **very last line**:
```
REPORT_SAVED: /workspace/report.md
```

## Pitfalls

- **Doing research yourself.** You are the lead. If you're calling
  `searcharvester-search` directly, stop and go to Phase 2.
- **Multiple `delegate_task` calls instead of one batch.** Use the
  `tasks=[...]` batch form so sub-agents run truly in parallel. Calling
  `delegate_task(goal=...)` N times is sequential.
- **Too broad sub-questions.** If a sub-agent returns thin findings,
  you didn't split finely enough. Target 4–6 extracts per sub-agent —
  less than 3 = over-broad.
- **Too many sub-questions.** More than 5 exceeds the concurrent
  budget and forces sequential batching. Merge overlaps.
- **Forgetting `toolsets=["terminal"]` in each task** — the sub-agent
  won't be able to run our search/extract scripts.
- **Free-form sub-agent output.** Always require the "Sub-question
  findings" template. Free prose is painful to merge.
- **Publishing the plan.** `/workspace/plan.md` is for context, not
  output. Only `/workspace/report.md` reaches the user.
