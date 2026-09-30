# Making a 9–14B Open Model Work Well: LLM Playbook

Sep 30, 2026 · @akshat

## Bottom line

A 9–14B model will not be "perfect" on its own, but wrapped in the right system it can be reliable on our narrow tasks: classify, extract, score and draft short text. The gain comes from the system around the model, not from the weights. This document covers only the LLM side and needs no fine-tuning to start.

The idea: make each model call small, constrained, checked and cheap to retry, and let a human approve anything that leaves the building. Use levers in this order, cheapest first, and stop when the eval gate passes:

1. Split big tasks into small single-purpose calls.
2. Force the output into a JSON schema (constrained decoding).
3. Write short, specific prompts with a fixed structure.
4. Add retrieved examples and knowledge instead of relying on memory.
5. Give the model tools (search, fetch, lookups) instead of asking it to recall facts.
6. Verify with code checks, retry once, then escalate.
7. Route each task to the smallest model that passes its eval.
8. Tune sampling and context settings per task.
9. Measure on our own eval sets; fine-tune only if a gate still fails.

What to expect: high reliability on structured tasks (qualify, extract, classify) once schema-constrained and checked; good-but-needs-review quality on emails; blog posts that are useful first drafts and always need a human edit for technical accuracy.

## Why small models fail, and what fixes each

Small models fail in predictable ways. Naming the failure tells you which lever to pull.

| Failure                            | Example in our project                    | Fix                                                                          |
| ---------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------- |
| Broken or extra output format      | JSON with trailing prose, missing field   | Constrained decoding (lever 2), schema validation                            |
| Invented facts                     | Made-up client, wrong AOSP version claim  | Retrieval and tools (levers 4–5), grounded-claims check                      |
| Loses focus on long input          | Misses the budget line in a long job post | Decompose, trim input, put the task last (levers 1, 3)                       |
| Inconsistent judgement             | Same lead scored 3 today, 8 tomorrow      | Low temperature, rubric with anchors, few-shot examples                      |
| Generic tone                       | Sounds like every cold email              | Retrieved past approved emails as examples, style card                       |
| Weak multi-step reasoning          | Contact hunt across several pages         | Code does the steps; the model does one step at a time                       |
| Over-agreeing                      | Says every lead is a fit                  | Rubric with explicit disqualifiers, negative examples                        |
| Prompt injection from scraped text | A page says "ignore instructions"         | Treat scraped text as data, schema-only outputs, no tool rights from content |

The last row matters: our agent reads untrusted web pages and emails. The model must never be able to send, publish or change a record just because text told it to. Actions happen only in code, after a schema-valid result and a human approval in Slack.

## Levers 1–5: shape the task and the input

**1\. Decompose.** One call, one job. "Read this page, find the company, decide fit, extract the contact and write an email" becomes five calls, each with its own prompt, schema and eval. Code passes results between them. Small models are far more reliable at a narrow step than at a long chain.

**2\. Constrain the output.** Serve with vLLM structured outputs so the model can only emit JSON matching a schema (enums for labels, bounded numbers, required fields). Keep fields few and ordered so the model states evidence before the verdict: evidence (quotes from the input), then score, then decision. Still validate in code; the schema guarantees form, not truth.

**3\. Prompt design.**

- Fixed structure: role in one line, task, rubric, output schema, then the input last.
- Rubrics with anchors: "score 8–10 only if the post names AOSP, BSP, HAL or bring-up work AND a delivery need", not "score how relevant it is".
- Explicit disqualifiers (staffing agencies, pure app development, unrelated hardware).
- Say what to do when unsure: return decision: "unsure" so a human sees it, never a guess.
- Keep prompts short and versioned in the prompt_versions table; change one thing at a time.
- Delimit untrusted text (&lt;page&gt;…&lt;/page&gt;) and state that it is data.

**4\. Retrieval and examples.** Pull 3–6 similar past cases (approved leads, sent emails that got replies, published posts) and our own knowledge docs (services, case studies, tone). Examples teach format, tone and edge cases better than added instructions, and they improve as the approval history grows, with no training. Cap total context so the input still fits comfortably. Details are in the Retrieval and Prompt Path document.

**5\. Tools instead of memory.** The model must not recall facts about a company or version. Code fetches the page, reads the careers feed, looks up the CRM, and hands text to the model. "Internet access" for us means our own fetch and search tools called by code (or by a bounded tool loop), with an allowlist, size limits, timeouts and robots.txt respect. A small model handles a loop of at most 3–5 tool calls well; beyond that, put the loop in code.

## Levers 6–9: check, route, tune, choose

**6\. Verify, retry once, escalate.** After every call, code runs checks: schema valid; quotes in evidence really appear in the input; email has an unsubscribe line and our address; no banned phrases or invented numbers; length within limits; every named client or claim exists in the knowledge docs. On failure, retry once with the error message appended. If it fails again, escalate to a bigger model, and if that fails, send it to Slack as "needs human". Log every attempt (ids and outcomes, no personal data).

**7\. Route by task.** Use the smallest model that passes that task's eval. Suggested start: a 9B dense model for qualify, extract and classify; the larger model only for blog drafts and hard replies. Routing lives in config/routing.yaml, so changing a model is a config edit, not a code change.

**8\. Sampling and context.**

- Structured tasks (qualify, extract, classify): temperature 0–0.2, fixed seed where supported.
- Drafting: temperature about 0.6–0.8, then the checks in lever 6.
- Set max_tokens per task so a runaway output cannot happen.
- Use only the context you need. Long context is expensive and small models degrade when the key fact is buried; cut pages to the relevant section in code before the call.
- If the model has a "thinking" mode, turn it on for hard judgement (reply drafting) and off for classification; measure both.

**9\. Model choice (9–14B class).** Pick by measured pass rate on our eval sets, not by leaderboard. Candidates already in this project: Qwen3.5-9B (dev default) and Gemma 4 12B; larger Qwen models are the escalation tier. Compare at least two on the same eval set before committing. Prefer models with a permissive licence and good support in vLLM for structured output.

## Recipes per task

Each task has its own prompt, schema, checks and eval set. Model tier is the starting guess, to be confirmed by the eval.

| Task              | Input (from code)                               | Output schema                                                                             | Retrieval                                    | Checks                                                                           | Start tier              |
| ----------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------- |
| Qualify lead      | Job-alert text or company page excerpt          | evidence\[\], score 0–10, decision (accept / reject / unsure), reason                     | 4 approved and 4 rejected leads              | Quotes exist in input; score matches decision band                               | 9B, temp 0              |
| Extract contact   | Cleaned text of contact, about and team pages   | contacts\[\]: role, email or form URL, source URL                                         | None, or 2 format examples                   | Email is on company domain; appears verbatim in page; syntax valid               | 9B, temp 0              |
| Draft first email | Lead facts, service pitch, sender               | subject, body, one specific reference to the lead                                         | 3 past emails that got replies; services doc | Length, unsubscribe line, postal address, no invented claims, one call to action | 9–14B, temp 0.7         |
| Classify reply    | Reply text and thread                           | label (interested / question / not now / unsubscribe / out of office / other), confidence | 3 examples per label                         | Unsubscribe always wins; low confidence goes to Slack                            | 9B, temp 0              |
| Draft reply       | Thread, classification, knowledge docs          | body, questions_for_human\[\]                                                             | Case studies, FAQs                           | No prices or dates unless in knowledge docs; human approves                      | 14B or larger, temp 0.5 |
| Blog plan         | Knowledge docs, past posts, topic list          | title, outline, target reader, sources_needed\[\]                                         | Past top posts                               | Topic is in scope; not a duplicate                                               | 9–14B                   |
| Blog draft        | Approved outline plus retrieved technical notes | sections\[\] with markdown                                                                | Our own notes, real documentation excerpts   | Every command or API name appears in a source; length; no fabricated benchmarks  | Largest model, temp 0.6 |

Three habits that matter most for each row:

1. **Evidence first.** Have the model quote the input before it decides. It reduces guessing and gives the checks something to verify.
2. **Unsure is a valid answer.** Every classifier can return unsure, which routes to Slack. Reviewing 10% of leads by hand beats trusting a wrong 100%.
3. **Technical blog posts are the riskiest output.** A small model will produce plausible but wrong AOSP or kernel details. Draft only from supplied notes, list sources_needed, and require an engineer to review each post before it is published.

## Serving with vLLM

Run vLLM as an OpenAI-compatible server on the company machine, reachable only over Tailscale, with an API key. The GPU host is powered on only for two scheduled windows (see the Architecture document, section 3), so throughput matters less than reliability: batch the window's calls, run them, and let the host power down afterwards. Start vLLM at boot (systemd) and allow 10–15 minutes for the model to load; the runner's health step waits for /v1/models before the first call.

vllm serve Qwen/Qwen3.5-9B \\  
\--served-model-name agent-dev \\  
\--api-key "\$LLM_API_KEY" \\  
\--max-model-len 16384 \\  
\--gpu-memory-utilization 0.90 \\  
\--host 0.0.0.0 --port 8001

Call it with the OpenAI client and pass a JSON schema so output is constrained:

resp = client.chat.completions.create(  
model="agent-dev",  
messages=\[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}\],  
temperature=0,  
max_tokens=600,  
response_format={"type": "json_schema",  
"json_schema": {"name": "qualify", "schema": QUALIFY_SCHEMA}},  
)

Practical points:

- **Memory.** Weights in bf16 take about 2 GB per billion parameters (a 9B model is roughly 18 GB), plus KV cache that grows with context length and concurrent requests. A 24 GB card fits a 9B model with a modest context and a few parallel requests; a 12–14B model needs a larger card or an 8-bit / 4-bit quantised build.
- **Quantisation.** Try an 8-bit build first (small quality loss), then 4-bit only if it still passes the eval. Always re-run the eval after changing precision.
- **Prefix reuse.** Keep the fixed part of each prompt (system text, rubric, schema) at the start and the variable input at the end so cached prefixes are reused where the server supports it.
- **Concurrency.** Cap parallel requests to what the card handles; queue the rest in the service.
- **Security.** Bind to the tailnet, require the API key, never open the port to the internet, and keep request logs free of personal data.
- **Pin versions.** Record the vLLM version, model revision (commit hash) and flags in models.yaml; a silent upgrade can change outputs.

Flag names and structured-output syntax change between vLLM versions, so confirm them against the installed version's docs before relying on this snippet.

## Evaluation

Without an eval set, "works well" is a feeling. Build the eval before tuning anything, and re-run it on every change to a prompt, model, quantisation level, retrieval setting or vLLM version.

**Eval sets** (in eval/sets/), labelled by a person:

- Qualify: 100 real job alerts or company pages, including hard negatives (agencies, unrelated hardware).
- Extract: 60 company sites with the correct contact known.
- Classify reply: 80 replies across all labels, including unsubscribes and out-of-office.
- Drafts (email, reply, blog): 20 each, scored by a human on a 1–5 rubric (accuracy, tone, specificity, safe to send).

**Metrics and starting gates** (tighten as data grows):

| Task           | Metric                                            | Gate to go live                           |
| -------------- | ------------------------------------------------- | ----------------------------------------- |
| Qualify        | Precision on accepted leads; recall of known-good | Precision ≥ 0.85, recall ≥ 0.80           |
| Extract        | Exact match on contact; zero wrong-domain emails  | ≥ 0.85 correct, 0 wrong-domain            |
| Classify reply | Accuracy; unsubscribe recall                      | ≥ 0.90 accuracy, unsubscribe recall = 1.0 |
| Draft email    | Human score; hard-check pass rate                 | Mean ≥ 4/5, 100% hard checks              |
| Blog draft     | Engineer review: factual errors per post          | 0 unfixed errors after review             |
| All            | Schema-valid rate; retry rate; escalation rate    | ≥ 99% valid; retries < 10%                |

The gate numbers are proposals to agree with the CTO, not measured results.

**Production feedback.** Every Slack approve, reject and edit is a labelled example. Log it, add it to the example store, and sample it monthly into the eval sets so they track reality. Watch the escalation rate and the human-edit distance on drafts; if either climbs, something changed.

## When to fine-tune, rollout and open items

**Fine-tune only when a gate still fails after levers 1–8**, and the failure is about style or consistency that examples cannot fix. Train small LoRA adapters on approved production data, on one shared base model. The full path is in the Fine-Tuned Models document; do not start it before the eval sets exist.

**Rollout checklist**

- ☐ Write the schemas and hard checks for qualify, extract and classify
- ☐ Serve the dev model with the API key on the tailnet only
- ☐ Label the eval sets (qualify 100, extract 60, classify 80)
- ☐ Run a baseline: bare prompt, no schema, no examples
- ☐ Add schema constraints, then rubric, then retrieved examples; re-run the eval after each
- ☐ Add verify, retry once and escalate; log outcomes
- ☐ Compare two models and one quantisation level on the same sets
- ☐ Set routing per task in config/routing.yaml
- ☐ Shadow mode: run on real alerts for a week with everything sent to Slack, nothing sent onward
- ☐ Go live per task only when its gate passes; keep human approval on email and posts

**Open items to verify before relying on this**

- Exact repo IDs of the larger Qwen models (marked TODO in models.yaml).
- Structured-output syntax and flag names for the installed vLLM version.
- Real memory use of each candidate on our GPU at our context length.
- Boot-to-ready time of the chosen model on our GPU, to size the warm-up grace period.
- Whether the chosen model's thinking mode helps or hurts each task (measure).
- Gate values, to be agreed with the CTO.
- Legal review of contact scraping and cold email (DPDP, CAN-SPAM, GDPR/CASL) is separate from model quality and still needed.