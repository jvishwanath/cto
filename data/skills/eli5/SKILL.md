---
name: eli5
description: Explain like I'm 5. Strip jargon, use analogies from everyday life, keep under 6 sentences.
---

You are running the **eli5** skill.

## Task

The user is asking: `$ARGS`

Respond as if explaining to a curious 10-year-old:

1. **One concrete analogy** drawn from kitchens, sports, traffic, board games, or
   playgrounds. Pick whichever fits. No "imagine a server" or "think of it as
   a function" — that's not ELI5, that's still tech-speak.
2. **No jargon.** If you must use a technical term, define it inline in 4 words
   or fewer. Words to avoid entirely: "abstraction", "paradigm", "leverage",
   "orchestrate", "asynchronous", "concurrent", "polymorphic", "encapsulation".
3. **Concrete examples, not categories.** Say "a vending machine" not
   "a state machine".
4. **Length cap: 6 sentences.** Hard limit. Long explanation = failed skill.
5. **No code blocks.** Words only.

If the user's question is already simple, still apply the analogy — that's the
whole point.
