---
name: api-explore
description: Research a third-party API by combining web search, doc fetch, and a quick scan for existing usage in the indexed corpus.
allowed-tools: [web_search, web_fetch, search_code, grep, read_file]
---

You are running the **api-explore** skill.

## Inputs

User passed: `$ARGS` — usually a library / API name and an optional
question (e.g. `stripe webhooks` or `kafka producer batching`).

If no args, call `ask_user` for the library / topic.

## Steps

1. **Internal-first** — before touching the web, run `search_code` and
   `grep` over the indexed corpus for `$1`. If this codebase already
   uses the API, surface the existing usage sites first (file + line).
2. **External research** — `web_search` for
   `"$1 official documentation"` or `"$1 $2 best practices"`. Prefer
   primary sources (project docs, RFCs) over blog posts.
3. **Deep read** — pick the single best URL and call `web_fetch` to
   pull the full text.
4. **Synthesize** — produce a brief (≤25 line) summary covering:
   - one-paragraph what / why
   - the 3 most important config knobs or API methods
   - one common pitfall (cite the source)
   - a 5–10 line minimal example IF the docs include one verbatim
5. **Link back** — finish with `Sources:` listing every URL you
   fetched, as markdown links.

## Constraints

- Cite. Every external claim must include the URL it came from.
- Don't fabricate code that wasn't in the fetched docs — if you can't
  find an example, say so and link to the API reference.
- This is a read-only research skill. No edits.
