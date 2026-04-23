---
name: searcharvester-deep-research
description: >
  Deep-research methodology with a ROLE-BASED team of parallel sub-agents.
  One `delegate_task` batch dispatches 2–3 researchers (each on a distinct
  sub-question), 1 critic (adversarial counter-search), and 1 fact-checker
  (numeric/date verification). The lead synthesises their findings into a
  cited report at ./report.md. Use for "research", "deep research", "report
  with sources", comparisons with citations, anything needing grounded
  multi-source evidence.
version: 2.3.0
author: Searcharvester
license: MIT
metadata:
  hermes:
    tags: [research, deep-research, delegate_task, parallel, subagents, citations, adversarial]
    category: research
    related_skills:
      - searcharvester-search
      - searcharvester-extract
      - subagent-driven-development
      - writing-plans
      - plan
---

# Searcharvester Deep Research (role-based)

Turn an open question into a cited markdown report grounded in real web
sources. Instead of N homogeneous sub-agents, you dispatch a small TEAM
with distinct roles — researchers gather, a critic tries to disprove,
a fact-checker verifies numbers. Save to `./report.md`.

## Role

You are the **lead researcher / editor**. You **do not personally run
searcharvester-search or searcharvester-extract** — the sub-agents do.
Your job:
1. Decompose the user query into 2–3 concrete sub-questions.
2. Dispatch ONE `delegate_task` batch of 4–5 sub-agents with explicit
   roles (see Phase 2).
3. Collect their findings, reconcile disagreements, write `./report.md`.

## When to use

- "Research / deep research / analyse X"
- "Compare A vs B with sources"
- "What's publicly known about [person / topic / company]"
- "Who holds the record for X" (factual questions benefit from the
  critic role — memory-based answers are often stale)
- "What's the latest on X"

**Do not** use this skill for: coding tasks, or questions you genuinely
cannot research online (private data, pure opinion).

## Core principles

- **Evidence before prose.** No claim in the final report without a
  source a sub-agent actually extracted.
- **Trust sources over memory.** Even "well-known" facts get stale.
- **Adversarial verification.** The critic's job is to be wrong on
  purpose — if they find contradictions, surface them.

## Procedure

### Phase 1 — Decompose (lead)

Write a plan to `./plan.md`:

```bash
cat > ./plan.md << 'EOF'
## Intent
<one sentence on what the user wants>

## Sub-questions (2–3)
1. <concrete, factually researchable>
2. ...

## Critical facts to fact-check
- <specific number/date/name that needs verification>

## Out of scope
- <things we won't cover>
EOF
```

### Phase 2 — Dispatch ONE batch with explicit roles (lead)

Call `delegate_task` ONCE with a team like this:

```python
delegate_task(
    tasks=[
        # Researchers — 2 to 3 of them, one per sub-question
        {
            "goal": "Researcher: sub-question 1 — <sub-question text>",
            "context": RESEARCHER_TEMPLATE.replace("<SUBQ>", "<q1>").replace("<USER_QUERY>", user_query),
            "toolsets": ["terminal"],
        },
        {
            "goal": "Researcher: sub-question 2 — <sub-question text>",
            "context": RESEARCHER_TEMPLATE.replace("<SUBQ>", "<q2>").replace("<USER_QUERY>", user_query),
            "toolsets": ["terminal"],
        },
        # Critic — searches for contradictions / alternative answers
        {
            "goal": "Critic: try to disprove the obvious answer",
            "context": CRITIC_TEMPLATE.replace("<USER_QUERY>", user_query),
            "toolsets": ["terminal"],
        },
        # Fact-checker — verifies specific numbers / dates / names
        {
            "goal": "Fact-checker: verify key numeric claims",
            "context": FACT_CHECKER_TEMPLATE.replace("<USER_QUERY>", user_query).replace("<FACTS>", "<list from plan>"),
            "toolsets": ["terminal"],
        },
    ],
)
```

---

### Template: RESEARCHER

```
ROLE: Researcher. You investigate ONE focused sub-question and return
a structured list of claims, each quoted from a real URL you read.

SUB-QUESTION:
<SUBQ>

PARENT USER QUERY (for context only):
"<USER_QUERY>"

TOOLS: You only have the `terminal` toolset. Call the searcharvester
scripts as shell commands — they are not registered as tools:

  python3 /opt/data/skills/searcharvester-search/scripts/search.py \
    --query "<query>" --max-results 5

  python3 /opt/data/skills/searcharvester-extract/scripts/extract.py \
    --url "<url>" --size m

HARD RULE: Your FIRST action must be a `terminal` call with search.py.
Never answer from your training memory — your data is older than today.

METHOD:
1. Run 2–4 search.py invocations with varied phrasings.
2. Pick 4–6 authoritative URLs from the combined results.
3. Run extract.py on each. If HTTP 422/502/500, try another URL.
4. Target: 4–6 successful extracts.

RETURN FORMAT (markdown only, no preamble):
### Findings
- **Claim**: <one-sentence factual claim>
  **Quote**: "<verbatim quote>"
  **URL**: https://...
- **Claim**: ...
(6–10 bullets total, each with a real URL)

### Notes
- Confidence: high / medium / low + one-line reason
- Most-recent source date: <YYYY-MM or "unknown">

IF YOUR OUTPUT HAS FEWER THAN 4 URLs YOU HAVE FAILED.
```

---

### Template: CRITIC

```
ROLE: Critic. Your job is to DISPROVE the obvious answer to the user's
query, or to show that the obvious answer is outdated / contested /
more nuanced than it appears. Assume the common / first-hit answer is
wrong until you have evidence that it's still right.

USER QUERY:
"<USER_QUERY>"

TOOLS: Same as researcher — terminal + searcharvester-search/extract
scripts.

HARD RULE: Your FIRST action is a search.py call with one of these
phrasings (pick what fits):
  - "<topic> record superseded"
  - "<topic> outdated"
  - "<topic> NOT <obvious answer>"
  - "<topic> corrections" / "<topic> debate"
  - For record-holder questions: search the CHALLENGER, not the
    incumbent. e.g. if query is "most X", search for "2024 most X
    champion" or names of recent competitors.

METHOD:
1. Run 3–5 adversarial search.py calls.
2. Extract 2–4 URLs that either (a) show a different answer or
   (b) confirm the obvious answer despite the adversarial framing.
3. Extract full content, not just snippets — headlines lie.

RETURN FORMAT:
### Counter-evidence
- **Counter-claim**: <alternative answer someone proposes>
  **Quote**: "<verbatim>"
  **URL**: https://...
  **Verdict**: plausible / weak / outdated / disproven

### Verdict
One of:
- **Obvious answer confirmed** — after adversarial searching, the
  obvious answer still stands. Report the adversarial queries tried.
- **Obvious answer superseded** — a newer / different answer has
  overtaken it. Name the new answer with sources.
- **Contested** — multiple sources disagree; surface the conflict.

IF YOU HAVE NO URLs IN YOUR RESPONSE YOU HAVE FAILED.
```

---

### Template: FACT_CHECKER

```
ROLE: Fact-checker. Verify specific numeric / date / name claims
related to the user's query. Your job is precision, not breadth.

USER QUERY:
"<USER_QUERY>"

FACTS TO VERIFY (from the lead's plan):
<FACTS>

TOOLS: Same as researcher.

HARD RULE: For each fact, run ≥2 searches from DIFFERENT angles, and
extract from ≥2 DIFFERENT DOMAINS. A single-domain confirmation is not
confirmation — big sites reprint each other's errors.

METHOD:
1. For each fact in the list, run 2 search.py calls with different
   phrasings that would reveal the correct value.
2. Extract from 2 authoritative URLs per fact (preferably official
   or primary sources: governing body, Wikipedia, major newspaper).
3. If sources disagree, note both values and flag the discrepancy.

RETURN FORMAT:
### Fact verification
- **Fact**: <claim>
  **Source A**: <quote> — <url>
  **Source B**: <quote> — <url>
  **Confirmed value**: <value>  (or "disputed: A says X, B says Y")
  **Date of source**: <YYYY-MM>
- ...

### Summary
- Confirmed: <n>
- Disputed: <n>
- Unverifiable: <n>

IF ANY FACT IS CONFIRMED BY ONLY ONE DOMAIN, MARK IT "[tentative — single source]".
```

---

### Phase 3 — Synthesise (lead)

When `delegate_task` returns:

1. Read each sub-agent's markdown block. Expect:
   - Researcher 1..N → `### Findings` bullets with URLs
   - Critic → `### Counter-evidence` + `### Verdict`
   - Fact-checker → `### Fact verification` + `### Summary`
2. Reconcile:
   - If critic's verdict is "confirmed", write the obvious answer with
     standard caveats.
   - If critic's verdict is "superseded", the NEW answer becomes the
     headline; write a "What changed" paragraph explaining the
     supersession with the dates.
   - If critic's verdict is "contested", write a "Disagreements"
     section surfacing both sides.
3. Cross-check numbers with the fact-checker. Any number only
   confirmed by one domain gets `[tentative — single source]` inline.
4. Build a unified reference list — dedupe URLs, number them [1]..[N]
   in order of first appearance.
5. Write `./report.md`.

```markdown
# <Title reflecting the actual question>

## TL;DR
<2–4 sentences. If answer is contested or changed, say so here.>

## <Sub-topic 1>
<Claims with inline [n] citations; tentative ones get
`[tentative — single source]`.>

## <Sub-topic 2>
...

## What changed / Disagreements
<Include ONLY if the critic found supersession or contest. Spell out:
- What was commonly believed.
- What sources now say.
- Why the newer answer wins (primary source, recency, etc.).>

## Caveats
<Known limitations, unanswered parts.>

## References
[1] <short label> — <URL>
[2] <short label> — <URL>
```

Hard rules:
- Every claim has an inline [n] citation.
- Same URL = same number.
- Claim from only 1 source → `[tentative — single source]`.
- If the critic or fact-checker surfaced a conflict, the
  "What changed / Disagreements" section is mandatory.
- Use tables for 3+ item comparisons.
- No marketing adjectives unless quoted.
- Numbers get units and dates.
- Match the user's language.

### Phase 4 — Deliver

Before closing:
- [ ] `./report.md` exists and > 400 bytes.
- [ ] Every non-TL;DR, non-References section has ≥1 inline `[n]`.
- [ ] References list has ≥ 8 unique URLs (target 15+).
- [ ] TL;DR is standalone (no citations, no "see below").
- [ ] Critic verdict is reflected somewhere in the report.

Then print as the very last line:

```
REPORT_SAVED: ./report.md
```

## Pitfalls

- **Skipping the critic.** Without it you get training-memory answers
  dressed up with confirmatory extracts. The critic's adversarial
  search is what catches stale / contested answers.
- **Researchers overlap.** Each researcher should own a distinct
  sub-question — if two researchers end up citing the same URL, you
  sliced the question wrong.
- **Multiple delegate_task calls.** Use the `tasks=[...]` batch form
  once. Calling `delegate_task(goal=...)` N times serialises the team
  and destroys the parallelism.
- **Accepting Hermes "(empty)" summaries silently.** If a sub-agent
  returns "(empty)" (model no-op), note it in the report's Caveats
  and do NOT pretend the research is complete.
- **Free-form sub-agent output.** Stick to the role templates. A
  researcher that writes prose instead of `Claim / Quote / URL` bullets
  is unusable.
- **Sub-agent writing from memory.** If a sub-agent's output has no
  URLs, it did not actually call extract.py — reject / note it.
