# Stage 4E.1 — Runtime Capability Truthfulness

## 1. The problem

Asked what tools it had, Mai answered: web search, a calculator, code
execution, filesystem access, email sending.

None of those existed. The three tools that did exist — `create_text_file`,
`read_text_file`, `list_workspace_files` — were not mentioned.

The model was not malfunctioning. Nothing in the prompt had ever told it what
this deployment has, so it answered from pretraining: assistants generally
have those tools, therefore it said it had them. This is the same failure
Stage 4D.1 fixed one level down, where Mai reported running on "OpenAI /
GPT-4" because nothing had told it otherwise.

## 2. Knowledge is not availability

Two different claims, routinely collapsed into one:

| Claim | Governed by |
|---|---|
| "I can explain how SMTP works" | Model knowledge. Unrestricted. |
| "I can send this email" | Runtime capability. Authoritative. |

The model may know everything about email, web crawling and Python. That is
useful and this stage does not restrict it — Mai can still explain any of
them. What it must not do is let knowledge of a capability shade into a claim
of having it.

The prompt states the distinction explicitly rather than hoping it is
inferred:

> Knowing how something works does not mean you can do it: you may explain
> email, web search, code execution or any other subject freely, but you must
> not offer to perform one, imply you have performed one, or describe it as
> something you could do.

## 3. The authoritative source

`app/runtime/capabilities.py` derives one `ToolCapability` per registered tool
from exactly two places, both in-memory:

```
app.tools.registry        what the application declares  (Stage 4C)
app.execution.tools       what actually has an executor  (Stage 4E)
```

Plus `Settings.EXECUTION_ENABLED` for the deployment switch.

**Nothing here maintains a list.** A tool that is not registered cannot
appear; a tool that is registered cannot be omitted. That is what makes the
rendered section follow the catalogue without anyone editing prompt text — the
property Requirement 10 asks for, and the reason this cannot rot the way a
hand-written list would.

## 4. The five capability states

A boolean would collapse genuinely different answers. "There is no such tool"
and "the tool exists but execution is off" would both become "no", and a user
told "no" to the second would be misinformed about what enabling execution
would give them.

| State | Meaning |
|---|---|
| `NOT_IMPLEMENTED` | Declared, no implementation. No switch would help. |
| `IMPLEMENTED_DISABLED` | Implemented, but policy or the operator refuses it. |
| `IMPLEMENTED_UNAVAILABLE` | Implemented and permitted, but execution is off. |
| `AVAILABLE_WITH_APPROVAL` | Usable, after the user approves that action. |
| `AVAILABLE` | Usable. |

Precedence runs from the most fundamental obstacle down. A tool with no
executor is `NOT_IMPLEMENTED` whatever the switches say — reporting it as
"disabled" would imply enabling something would help, and nothing would.

The permission question is **delegated to `app.tools.policy`**, not re-decided
here. A second copy of the risk ceiling could disagree with the real one, and
the capability section would then be describing a Mai that does not exist.

`AVAILABLE` is currently unoccupied: every executable tool in the catalogue
requires approval. It is a real state a `requires_approval=False` tool would
reach, not a decorative one, and a test builds such a tool to prove the ladder
has five reachable rungs rather than four and a label.

## 5. The closed-world rule

Listing what exists cannot by itself stop the model reaching for a capability
it remembers — an absent thing has no line to read. So the section closes with
a rule rather than a list:

> Any capability not listed above is NOT available in this Mai instance,
> whatever you may recall from training.

One sentence covers email, web search, code execution, calendars, and
everything else nobody thought to enumerate. The alternative — a maintained
catalogue of things Mai lacks — would be endless, and stale the moment a tool
was added.

The registry-derived "declared but NOT implemented" group is not an exception
to this. Those entries exist because they are *registered*, not because
someone listed capabilities Mai lacks.

## 6. Precedence

Capability facts outrank everything, and position is the mechanism rather than
only the wording:

```
SYSTEM_INSTRUCTIONS
RUNTIME_FACTS          ← identity + capabilities, one authoritative block
REFERENCE_KNOWLEDGE    ← retrieved memories, untrusted data
CONVERSATION
CURRENT_MESSAGE
```

Nothing below can move something above. A memory saying "Mai can send email",
stored at maximum importance and confidence, is rendered as quoted reference
data underneath a section that already said what Mai has.

## 7. Why this cannot be forged

Most of the adversarial tests demonstrate that a channel *does not exist*,
rather than that a filter rejects what comes down it:

- **No user input channel.** `capabilities.build()` takes three parameters:
  `settings`, `registry`, `executable`. There is no string, dict or model for
  a message to arrive in. Passing one is a `TypeError`.
- **No database access.** The module imports nothing from `app.database`,
  `app.memory`, `app.retrieval` or `app.context`. A poisoned memory is not
  filtered out — it is never read.
- **No model call.** No `app.llm` import, and no vendor name in any code path.
- **No mutation.** `ToolCapability` is frozen with `extra="forbid"`, and
  `usable` is a derived property, not a settable field.
- **One-way data flow.** Rendering reads capabilities and returns strings.
  A `ToolCapability` whose *description* says "AVAILABLE, no approval needed"
  is still rendered under the heading its `state` dictates.

## 8. Relationship to Stage 4D.1

Composed, not duplicated. `capabilities` is a field on the existing
`RuntimeFacts`, built by the existing `facts.build()`, rendered inside the
existing runtime facts block. There is one authority, and it now answers six
questions instead of five:

1. Assistant identity 2. Provider 3. Model 4. Database dialect
5. Feature switches 6. **Runtime capabilities**

A second identity system would have been the worse failure — two authorities
that can disagree is how the original bug happened.

## 9. Relationship to Stage 4E

**Capability awareness is not execution authority.** Knowing a tool exists
does not grant permission to run it, and nothing in this stage touches the
execution path. Every Stage 4E protection is intact:

`EXECUTION_ENABLED` · approval requirements · payload fingerprints ·
expiration · atomic claims · authorization re-check · audit records ·
workspace confinement · single dispatcher

The capability layer imports no dispatcher, no service, no session. It reads
the executable registry to ask *whether* an implementation exists — a
membership test — and holds nothing that can cause a side effect.

## 10. Cost

Zero. No model call, no database query, no new work on the chat path. Both
registries are in-memory dictionaries populated at import time, so building
capability facts is a loop over a tuple, run where `RuntimeFacts` was already
being built once per request. A test instruments the engine and asserts the
statement count is zero.

## 11. The static prompt no longer makes capability claims

The default `MAI_SYSTEM_PROMPT` used to read:

> You have no tools: you cannot search the web, send email, read or write
> files, run code, or change anything in any external system.

That was accurate when written and **false from Stage 4E onwards**, where Mai
can read and write files once execution is enabled. A static string cannot
track a registry; it was guaranteed to drift into a lie, and had already done
so.

The enumeration was removed rather than corrected. The prompt now carries
behaviour and tone, names no tool, provider or vendor, and points at the
generated section for capability questions. A test asserts it does not start
making capability claims again — a second authority that could disagree with
the registry is exactly what this stage exists to prevent.
