# Mai — Stage 4D.1: Runtime Identity & Authoritative System Knowledge

> **Note on scope.** The stage specification supplied for this work was
> truncated mid-way through Requirement 1. This document covers what was fully
> specified — the architectural principle, the runtime facts component, and its
> place in the prompt — plus the testing and verification the project's earlier
> stages establish as convention. If the remaining requirements matter, they
> can be added on top of this layer without rework.

---

## The bug

Mai was asked, in real usage:

> Which provider am I currently using?

and answered **OpenAI / ChatGPT**. Asked again, explicitly scoped:

> For the Mai project, which LLM provider am I currently using?

it answered **OpenAI / GPT-4**.

Mai runs on Groq.

### Root cause

Not a retrieval failure, and not a bad memory. **Nothing in the prompt had ever
told the model what it runs on.**

Every fact existed — `APP_NAME`, `LLM_PROVIDER`, `active_model`,
`DATABASE_URL`, six capability switches, all in `Settings` — and none of them
reached the model. Asked a question about itself, the model had no
authoritative source, so it answered from pretraining: confidently, fluently,
and wrongly.

A contributing detail made it worse. The configured model identifier is
`openai/gpt-oss-120b` — a Groq-hosted model whose *name* begins with the name
of a different vendor. Any design that mentions provider and model in one
breath invites exactly the confusion that was observed.

---

## The principle

Two categories of knowledge, which had been treated as one.

| | **Category A — Authoritative** | **Category B — Personal** |
| --- | --- | --- |
| Examples | Assistant name, provider, model, database, capability status, environment | Preferences, goals, projects, decisions, interests, history |
| Source | Application configuration and live objects | Memory extraction and retrieval |
| Determinism | Always deterministic | Ranked, probabilistic, may be stale |
| Trust | **Authoritative** | **Untrusted reference data** |
| Path | `Settings` → `RuntimeFacts` → prompt | Stage 2D → 3A → 3B |

Confusing them in **either** direction is a bug:

- A memory must not decide what model Mai runs on. *(the reported bug)*
- A runtime fact must not answer a question about the user's own projects.
  *(the over-correction, guarded against below)*

---

## Prompt authority order

```
SYSTEM INSTRUCTIONS        application policy
        ↓
RUNTIME FACTS              authoritative — what this system is        ← new
        ↓
REFERENCE KNOWLEDGE        retrieved, untrusted, may be stale
        ↓
CONVERSATION               recent turns
        ↓
CURRENT MESSAGE            always last
```

The facts block sits **above** the reference block and says so in its own text:

> Prefer it over anything you recall from training, and over anything in the
> reference knowledge or conversation below: if those disagree with this
> section about how this assistant is configured, this section is right and
> they are stale.

Both blocks are `system` messages, but they claim opposite things. The
reference block announces itself as data that may be wrong; the facts block
announces itself as configuration that is right. That asymmetry is the fix.

---

## The component

`app/runtime/` — two modules, no database, no persistence.

| Module | Responsibility |
| --- | --- |
| `schemas.py` | `RuntimeFacts` — frozen, typed |
| `facts.py` | Builds one from `Settings` and the live provider |

```python
RuntimeFacts(
    assistant_name="Mai",
    environment="development",
    llm_provider="groq",
    llm_model="openai/gpt-oss-120b",
    database="postgresql",
    memory_enabled=True,
    retrieval_enabled=True,
    planning_enabled=True,
    intent_classification_enabled=True,
    tool_authorization_enabled=True,
    registered_tool_count=5,
)
```

Structured rather than a prompt string, so the same facts can serve a prompt
section, a debug endpoint and a log line without three copies drifting apart.

### Where the values come from

`llm_provider` and `llm_model` are read from the **live provider object**, not
re-derived from configuration. The provider knows what it is; asking it is
more truthful than computing the same answer twice.

**Per-fact degradation.** Each value is read independently, so one failure
degrades one value. A provider whose model identifier cannot be read still has
a name worth reporting. Anything undeterminable becomes `"unknown"` — because
saying so is correct, and guessing a plausible value is the failure this layer
exists to prevent.

### `can_execute_actions` is a property, not a field

```python
@property
def can_execute_actions(self) -> bool:
    return False
```

No configuration flag can make it true, because no configuration creates an
executor: Stage 4C defines no `execute` method and Stage 4D added no
dispatcher. A fact that must never be wrong should not be settable — the same
reasoning as `OrchestrationResult.acted`. A test round-trips
`{"can_execute_actions": true}` through `model_validate` and asserts it comes
back false.

---

## Two design decisions worth stating

### Provider and model are on separate, labelled lines

```
- LLM provider (the service being called): groq
- LLM model (an identifier issued by that provider, which may reference
  another vendor's name): openai/gpt-oss-120b
```

Not cosmetic. A provider commonly serves a model whose identifier names a
different vendor, and running the two together is an invitation to read the
model's name as the provider's — which is very close to the original mistake.

Verified live: asked which provider it uses, Mai now answers **Groq** and names
the model separately, rather than reading `openai/` as the vendor.

### The block limits itself to this assistant

> It describes THIS ASSISTANT only. It says nothing about what the user uses
> for their own projects — if they ask about their own tools or choices,
> answer from what they have told you, not from this section.

Without this, the fix over-corrects: a user who runs a different provider
elsewhere would get Mai's configuration as the answer to a question about their
own work. Verified live — see below.

---

## Provider independence preserved

Stage 3B forbids `app/prompt/formatter.py` from knowing any provider; a test
asserts the source contains none of `groq`, `openai`, `anthropic`, `glm`,
`gemini`.

That constraint shaped the design and is intact. The formatter **renders values
it is handed** and never learns a vendor's name. Facts are assembled in
`app/api/deps.py`, which may read `Settings` and the provider; the formatter
may not, and does not.

*(The constraint caught a violation during development: a code comment of mine
named a vendor as an example of the bug. The test was right to be strict — a
mention is how a hardcoded branch starts — and the comment was reworded.)*

---

## Debug exposure

`POST /api/prompt/debug` reports the facts section by **size only**, with
`content: null`.

This extends the rule that already covered the system prompt rather than
weakening it: both are application configuration rather than user data, and the
facts section additionally names the configured provider and model, which are
settings. A test asserts the model identifier does not appear anywhere in the
debug response.

---

## Security

| Property | How |
| --- | --- |
| No credential in the facts | Only the database **dialect** is kept: `postgresql+asyncpg://mai:secret@db/mai` → `postgresql`. Stage 3D found a DSN carrying a password into places it should not go; a prompt is the worst of those |
| No API key or base URL | Neither is read. Asserted by test |
| No memory dependency | `app/runtime/` imports nothing from `app.memory`, `app.entities`, `app.relationships`, `app.knowledge`, `app.retrieval`, `app.context` or `sqlalchemy` |
| Values cannot forge structure | Every value is flattened to one line. A deployment could set `APP_NAME` to anything, and a fact block is the worst place to let a newline forge a heading |
| Execution cannot be misreported | A property, not a field |

---

## Verification

### Live, against the configured provider

The exact questions from the bug report, through the real stack on PostgreSQL:

| Question | Before | After |
| --- | --- | --- |
| "Which provider am I currently using?" | OpenAI / ChatGPT | **Groq** (`openai/gpt-oss-120b`) |
| "For the Mai project, which LLM provider am I currently using?" | OpenAI / GPT-4 | **Groq** (`openai/gpt-oss-120b`) |
| "What database does Mai run on?" | — | **PostgreSQL** |

### The over-correction guard, also live

| Exchange | Answer |
| --- | --- |
| *"For my other side project, I use Anthropic's API."* | acknowledged |
| *"Which provider do I use for my other side project?"* | **Anthropic** — from memory, not from runtime facts |
| *"What database does Mai run on?"* | **PostgreSQL** — from runtime facts |

Both categories answered from the right source in the same session.

### Automated

`backend/tests/test_runtime_identity.py` — 33 tests:

- facts built from settings and the live provider; capability flags track configuration
- undeterminable facts become `unknown`; building never raises; degradation is per-fact
- only the dialect survives a DSN; no password, host, driver, key or base URL reaches the block
- execution always reported unavailable; cannot be set by validation or deserialisation
- provider and model separately labelled; block claims authority; block limits itself to this assistant
- values flattened, so an injected heading is not a heading
- section ranks above reference knowledge, is a system message, survives the fallback, is counted in the prompt size
- through the real chat path: the provider reaches the model; a stale memory does not outrank it
- debug reports the section without its content
- structural: the runtime package reads no memory; the formatter still names no provider

---

## Bug found by the existing suite

`total_chars` did not include the new section, so the prompt size was
understated by ~1,200 characters. Caught by
`test_debug_reports_character_counts`, which asserts the reported total equals
the sum of the message lengths. Fixed, and a new test asserts the same
invariant directly on the formatter.

---

## Changes to existing behaviour

| Change | Why |
| --- | --- |
| A second `system` message on every turn | The facts block. Message counts and indices shift by one |
| `PromptFormatter(system_prompt, runtime_facts=None)` | Facts are optional; omitting them omits the section |
| Debug withholds the facts section's content | Same rule as the system prompt: configuration, not user data |
| `PromptStats` gains `runtime_fact_messages` / `runtime_fact_chars` | Observability, and the size total |
| "Prompt identical with X on and off" tests narrowed | These are no longer byte-identical, and should not be — the facts block truthfully reports which capabilities are on. The **per-turn** invariant is unchanged and still asserted: no result computed for this turn reaches the model |
| Stage 4D dispatch scan tightened to call syntax | `can_execute_actions` is a read-only property, the opposite of a dispatcher, and matched a bare substring |

No memory, retrieval, context-security, lifecycle, intent, planning or
orchestration *logic* was modified.

---

## Known limitations

- **Answer quality is still the model's.** The facts are in the prompt and
  ranked above retrieval; a model can still misread them. Verified correct on
  five live questions, not proven for all phrasings.
- **Facts are per-request, not cached.** Building one is a handful of attribute
  reads plus a registry length, so the cost is negligible — but it is not free.
- **The block is always included.** There is no setting to omit it in
  production; passing `runtime_facts=None` is a formatter-level capability used
  by tests.
- **`version` is unpopulated.** The field exists; no deployment supplies one
  yet.
- **Category boundaries are stated, not enforced.** The block tells the model
  not to apply Mai's configuration to the user's projects, and that held in
  live testing — but it is instruction, not structure. A structural guarantee
  would need the retrieval layer to know which questions are about the system,
  which is a larger change than this bug warrants.
