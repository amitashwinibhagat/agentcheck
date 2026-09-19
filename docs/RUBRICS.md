# Writing rubrics

A rubric is a YAML file that tells the judge what to check. It is the product:
the judge is replaceable plumbing underneath it.

Drop a rubric in one of these directories and it is available everywhere —
CLI, API, and UI — by name:

1. `./checksets/` (the repo you're working in)
2. `$AGENTCHECK_CHECKSETS` (colon-separated directories)
3. `$AGENTCHECK_HOME/checksets/` (default `~/.agentcheck/checksets/`)

Built-in rubrics cannot be shadowed; name yours something else.

## Schema

```yaml
name: my-rubric            # required, unique, becomes the lookup name
description: >             # optional, shown in `checkset show` and the UI
  What this rubric decides.
verdict: decision          # which check id carries the overall verdict
severity: severity         # optional; must be a score check
checks:                    # required, 1..n
  - id: my_check           # required, unique, slug-style
    type: noul             # noul | choice | score
    instructions: >        # required — the question the judge answers
      Was the order placed inside the return window?
  - id: decision
    type: choice
    instructions: How should this be handled?
    criteria:              # choice: a MAP of option -> description
      approve: inside policy, refund directly
      escalate: a human decides
      deny: outside policy
  - id: severity
    type: score
    instructions: How bad if this is wrong?
    criteria:              # score: an ORDERED LIST of levels, low -> high
      - trivial
      - annoying
      - costly
```

## The three check types

| Type | Returns | Use for |
|---|---|---|
| `noul` | probability 0–1 | yes/no questions: "is this inside policy?", "did it exfiltrate?" |
| `choice` | one option + probabilities + confidence | the verdict itself: approve / escalate / deny |
| `score` | a number on your scale + confidence | severity, quality, risk |

**Exactly one `choice` check must carry the verdict.** If the rubric has only
one choice check you can omit `verdict:` and it is inferred; with two or more
you must name it. Same rule for `severity` (a `score` check), except severity
is optional.

## Rules the linter enforces

`agentcheck checkset lint <file|name>` exits non-zero on:

- missing `name`, empty `checks`, unknown `type`, missing `instructions`
- duplicate check ids
- a `choice` whose `criteria` is not a map, or a `score` whose `criteria`
  is not a list (this asymmetry is the vendor's wire format — the linter
  catches it before an eval burns quota on a 422)
- a `verdict`/`severity` pointer that names a missing id or the wrong type
- no `choice` check to carry the verdict

## Lint in CI

Drop this in `.github/workflows/rubrics.yml` so a broken rubric cannot merge:

```yaml
name: rubrics
on:
  pull_request:
    paths: ['checksets/**.yaml', 'checksets/**.yml']
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - run: pip install agentcheck
      - run: |
          set -e
          for f in checksets/*.yaml; do agentcheck checkset lint "$f"; done
```

## Worked example: a content-moderation rubric

```yaml
name: comment-moderation
description: >
  Should this comment be published? Three-way branch maps onto what
  moderation actually needs: publish, hold for a human, remove.
verdict: action
severity: harm

checks:
  - id: is_spam
    type: noul
    instructions: >
      Is this comment promotional content, link spam, or a bot post?
  - id: is_abusive
    type: noul
    instructions: >
      Does this comment harass a person, use slurs, or threaten anyone?
  - id: action
    type: choice
    instructions: How should this comment be handled?
    criteria:
      publish: normal participation, no issues
      hold: rude or heated but not clearly rule-breaking; a human looks
      remove: harassment, hate speech, threats, or spam
  - id: harm
    type: score
    instructions: How much harm would leaving this up cause?
    criteria:
      - none
      - mild
      - serious
```

Then check a comment with it:

```bash
agentcheck check --key $AGENTCHECK_KEY \
  --trace comment.json --checkset comment-moderation
```

## Tips

- **Write instructions as a question the judge answers about the state**, not as
  an instruction to the agent. "Was the order inside the window?" not
  "check the window".
- **One idea per check.** Five atomic checks beat one compound question —
  batching makes them one call, and you see *which* one flagged.
- **Option descriptions separate the options.** The judge reads both the name
  and the description; "approve: inside policy and refundable" beats
  "approve: yes".
- **Score levels are ordered low → high** and the judge returns a number in
  that range. Describe the levels, not just name them.
- **Start with a string.** `instructions` and each criteria entry can also be an
  object or array for multi-part guidance, but a sentence is the right start.
