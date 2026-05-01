# HistoryBites — Decisions Log

This file mirrors the canonical Decisions Log in Notion:
https://www.notion.so/34a52c14aa53815a9b3ce27168f9b8f7

The Notion version is the source of truth. This mirror exists so contributors
can grep/diff decisions without leaving the editor. Update both when adding
new decisions.

Append-only record of architectural decisions and their reasoning. The point
of this page is so Claude Code (and future Will) understands **why** things
are the way they are, and doesn't waste time re-litigating settled questions.

Format: one decision per section. What we chose, why, what we rejected.

---

## D1 — Wikipedia is the primary source for v1

**Decision:** Build only the Wikipedia adapter for v1. Other sources are post-launch.

**Why:** Wikipedia's CC BY-SA 4.0 license is unambiguous, the API is best-in-class, and non-English editions solve the cultural diversity problem on their own in Phase 5. Adding multiple sources on day one is premature abstraction.

**Rejected:** Starting with a multi-source pipeline. Building a `Source` protocol with no second implementation.

---

## D2 — Pick the article first, summarise second

**Decision:** The pipeline always selects a specific Wikipedia article first, then feeds its extract to Gemini and asks for a surprising fact. Gemini never freelances or "searches."

**Why:** Guarantees a real, verifiable source URL. Eliminates hallucinated history. Makes copyright compliance tractable (we know exactly what Gemini saw).

**Rejected:** "Ask Gemini for a surprising history fact" with no source grounding.

---

## D3 — Uniqueness enforced at topic level, not semantic

**Decision:** Store `wikipedia_page_id` (as `external_id`) with a UNIQUE constraint per source. Never reuse the same article. Semantic variety is handled separately by category/era/region sampling.

**Why:** Semantic uniqueness ("don't run 3 battles in a row") is a vector-similarity problem that's overkill for this use case. Topic-level uniqueness via a unique index is one line of schema and covers the actual failure mode.

**Rejected:** Embedding every fact and doing cosine similarity against history.

---

## D4 — Cultural diversity is a sampling problem

**Decision:** Curate a list of ~30-50 `(wikipedia_category, region, era)` tuples. Pick uniformly at random. To increase weight on underrepresented areas, add more tuples covering them.

**Why:** English Wikipedia's random endpoint skews US/UK/sports because that's where the editor gravity is. Curated categories give direct control. No explicit weights — tuple count is the weight.

**Rejected:** Wikipedia random endpoint with post-hoc filtering. Explicit numeric weights (easier to tune by adding/removing tuples).

---

## D5 — Copyright compliance: facts vs. expression

> **Superseded in part by D20 —** the n-gram validation step described below has been removed. The prompt instruction and human review gate remain.

**Decision:** The v1 Gemini prompt explicitly instructs "Do not copy phrasing from the source. State the fact in your own words in one sentence." ~~A validation step rejects any output containing an 8-word consecutive substring from the source extract.~~ *(n-gram check removed per D20.)*

**Why:** Copyright protects expression, not facts. Close paraphrasing is infringement even with attribution. ~~The n-gram check is cheap and catches the failure mode.~~ *(See D20 for the rationale.)*

**Rejected:** Relying on attribution alone. Trusting Gemini to paraphrase without a validation gate.

---

## D6 — Two tables (facts, pool), not one

**Decision:** Separate `facts` and `pool` tables with near-identical schemas.

**Why:** A single table with a nullable `scheduled_date` means every query has to remember whether it wants pool rows, delivered rows, or both. Forget once and you get bugs. Two tables have unambiguous semantics.

**Trade-off:** Schema changes require touching both tables. Cross-table uniqueness (same article in both) enforced in application code, not DB.

**Rejected:** Unified table with lifecycle column. "Clever" but accident-prone.

---

## D7 — Generate ahead, don't generate just-in-time

**Decision:** Maintain a pool of pre-generated facts. Scheduling pops from the pool into `facts` for tomorrow. Delivery is a boring DB read.

**Why:** Decouples generation from delivery. A Gemini outage at 9am doesn't miss a day — tomorrow's fact was already generated hours or days ago.

**Rejected:** Generating today's fact in the request handler. Generating at 9am and delivering immediately.

---

## D8 — Three-tier approved buffer, every 6h cron

**Decision:** Three runtime constants govern the approved-pool buffer:

- `APPROVED_TARGET = 7` — target buffer depth (≈7 days of unattended operation)
- `APPROVED_ALERT_THRESHOLD = 3` — Slack-alert floor
- `REVIEW_QUEUE_TARGET = 20` — pending_review queue size the generation cron fills toward

Run the generation cron every 6h.

**Three-tier operational state for the approved pool:**

- `approved >= 7` → ok (target buffer met)
- `3 <= approved < 7` → warm (below target but not alerting; topup loop continues filling pending_review for human review)
- `approved < 3` → low (Slack alert fires every cron run until threshold restored)

**Why (buffer depth = 7):** 7 days recovers cleanly from any realistic outage — Will away for a week, Gemini outage, prompt iteration broke something. Longer buffers lock in prompt/category mistakes for longer and mask generation failures. Shorter feedback loop is better for a solo dev iterating.

**Why (alert floor = 3):** 3 days is genuine urgency — below this, Will needs to act before tomorrow's pin runs out of approved candidates. Above this, daily cron has time to recover via human review velocity.

**Why (review queue target = 20):** Generation cron fills pending_review toward 20 to give Will a meaningful batch to review without hand-cranking generates one at a time. 20 is comfortable for a single review session; higher would be intimidating.

**Why (cron frequency = 6h):** Idempotent jobs are cheap. 6h means max 6h to detect + recover from a broken run. Daily would be simpler but creates a 24h blind spot.

**Rejected:** 30-day buffer (too slow to iterate). Hourly cron (unnecessary). Single-tier alerting (alert OR no-alert) — three tiers give operational signal without doubling alert volume.

---

## D9 — Human review queue

> **Modified by D23 —** human review now applies to BORDERLINE judge verdicts only. HIGH-confidence facts auto-approve, LOW-confidence auto-reject. The review UI built in Step 8 functions as designed; it just receives fewer items.

**Decision:** Generated facts go into `pool` with `status='pending_review'`. Will reviews weekly via `/admin/review` HTML page. Only `approved` items can be scheduled into `facts`.

**Why:** Quality control for v1. Even with a good prompt, Gemini will occasionally produce bland or weird output. A human gate catches this before users see it.

**Rejected:** Programmatic validation only. Sync pool to Notion for review there (more moving parts, more to break).

---

## D10 — Wikipedia categorymembers, not search or SPARQL

**Decision:** Use Wikipedia's `categorymembers` endpoint to list candidate articles.

**Why:** Precise and controllable. Curated category seeds map directly. Keyword search is fuzzy. Wikidata SPARQL is powerful but significantly more complex and is a Phase 5 enhancement.

**Rejected:** Wikipedia search endpoint (too fuzzy). Wikidata SPARQL (premature).

---

## D11 — stdlib logging, not a custom singleton

**Decision:** Configure Python's `logging` module once at startup. Each module: `logger = logging.getLogger(__name__)`.

**Why:** Python's `logging` module is already a singleton by design. Writing a `LogManager` class is un-Pythonic, considered a code smell, and reinvents stdlib. Modules themselves are singletons in Python.

**Rejected:** Custom logging singleton class.

---

## D12 — Firebase Analytics client-side, not custom server endpoint

**Decision:** Use Firebase Analytics on the Android side. No server-side analytics endpoint.

**Why:** Industry standard. Free. DAU/session/custom events work out of the box. Building a server-side `/event` endpoint is reinventing the wheel and adds load.

**Rejected:** Custom analytics pipeline.

---

## D13 — In-memory cache on /today, Cloudflare at public launch

**Decision:** Cache `/today` responses in-memory for 5 minutes. Add Cloudflare in front of Railway at public launch.

**Why:** Everyone gets the same fact — one DB query per 5-min window regardless of user count. Cloudflare is the actual viral-scale defence. Both are cheap insurance.

**Rejected:** Redis cache (overkill for a single-instance app). Scaling Railway vertically as first response.

---

## D14 — No source abstraction until source #2

**Decision:** `wikipedia.py` is concrete, not behind an interface. Create the abstraction only when adding Wikidata or Smithsonian.

**Why:** Premature abstraction is worse than no abstraction. One implementation behind an interface gives no benefit. When source #2 exists, the interface falls out naturally from the diff.

**Rejected:** Building `Source` protocol with just Wikipedia on day one.

---

## D15 — Timezone handling deferred

**Decision:** Server runs UTC. `scheduled_date` is naive date, interpreted as AWST by both server and app. "Today" rolls over whenever the server thinks it does.

**Why:** Single-region app for v1. International timezone handling is a Phase 5+ problem when we know if anyone cares.

**Rejected:** Per-user timezone support, multi-region delivery windows.

---

## D16 — Model abstraction from day one, Gemini for production

**Decision:** Define a `ModelProvider` Protocol with two implementations: `GeminiProvider` (used in production) and `OllamaProvider` (used locally for development against Gemma 4 on Apple Silicon). Production runs Gemini.

**Why:** At our volume (~5 generations/day), Gemini 2.5 Flash costs ~$0.36/year. Self-hosting Gemma 31B in production would cost ~$1,750/year on cloud GPU — 5,000× more expensive than the API for the same workload. But Gemma 4's Apache 2.0 license is genuinely appealing as future insurance. The abstraction gets us both: API simplicity in production, plus the ability to swap to hosted-Gemma or self-hosted-anything as a one-day refactor if Gemini ever becomes problematic. Local Ollama provider also lets us iterate prompts during development without burning API quota.

**Rejected:** Self-hosting Gemma 31B in production (5000× more expensive than the API for our volume; massive operational complexity). Direct Gemini SDK calls scattered across the codebase (lock-in for no benefit).

---

## D17 — FCM topic-based push for daily delivery

**Decision:** Notification delivery uses Firebase Cloud Messaging (FCM) topics. The Android app subscribes to a single `daily-fact` topic on first launch; the server pushes one message to the topic at 08:00 AWST daily; FCM fans out to all subscribers. WorkManager-based scheduled notifications are removed.

**Why:** WorkManager-based scheduled notifications are unreliable across non-Pixel Android devices — Samsung, Xiaomi, Oppo, Huawei, Vivo all aggressively kill background work via custom battery optimization. ~30-50% of installs would have unreliable delivery, which kills the product (the whole pitch is "morning notification"). FCM high-priority messages bypass Doze on virtually all OEMs because Google's messaging service is too important to break. Topics specifically (vs per-device tokens) eliminate the need for a devices table, registration endpoint, and token management — we have no per-user targeting need since everyone gets the same fact at the same time. FCM is free with no usage limits on the Spark tier.

**Trade-off:** Drops user-selectable notification time. Everyone gets the fact at 08:00 AWST. Strengthens the "shared moment" product story (D7) at the cost of one piece of customization. Timezone bucketing is a clean upgrade later — same architecture, just multiple topics like `daily-fact-utc-plus-8`.

**Rejected:** WorkManager-based scheduling (the actual root cause being solved). Per-user FCM tokens with per-user scheduling (premature complexity, requires timezone handling we deferred in D15). Hybrid FCM-wakes-WorkManager (doesn't actually solve the underlying battery-optimization issue).

---

## D18 — Retract endpoint and is_retracted column

**Decision:** Add `is_retracted BOOLEAN NOT NULL DEFAULT FALSE` to the `facts` table. Add `POST /admin/retract/{date}` endpoint (bearer auth) that sets `is_retracted = TRUE`. The `/today` endpoint excludes retracted facts when looking up today's row and when falling back to latest available.

**Why:** A product whose pitch is "a fact you can trust every morning" cannot ship without a way to take down a fact that turns out to be wrong, offensive, or a Gemini hallucination. Without this endpoint, fixing requires hand-rolling SQL against production, which is fragile and risks worse mistakes during an incident.

**Rejected:** Hard-deleting the row (loses the audit trail of what was published; breaks the `UNIQUE (scheduled_date)` constraint if we ever needed to publish a replacement). Manual SQL only (operational risk during an actual incident).

---

## D19 — Mirror Decisions Log to backend repo as DECISIONS.md

**Decision:** Maintain `DECISIONS.md` in the backend repo as a copy of this Notion page. Update both when decisions change.

**Why:** Notion is the authoritative human-readable home for architectural rationale (cross-linking, formatting, easy editing). But Notion-only creates a bus factor: if the workspace is corrupted, access lost, or Notion goes down during a 2am debugging session, the rationale is gone. The repo copy is also what Claude Code can read directly without leaving the codebase context. Cheap insurance.

**Rejected:** Notion-only (single point of failure). Repo-only (Notion is better for human reading + collaboration).

---

## D20 — Drop n-gram copyright validation (supersedes part of D5)

**Decision:** Remove the 8-word n-gram substring check from the generation pipeline. Copyright safety in v1 relies on two layers: (a) the prompt explicitly instructing Gemini to restate in its own words, (b) human review (D9) gating publication.

**Why:** The n-gram check catches verbatim copying but creates false confidence. Trivial paraphrases ("The king died in 1453" → "In 1453, the king died") bypass an 8-word consecutive-substring check while remaining borderline derivative. If we ever needed to defend our copyright posture, "we have a regex" reads worse than "we have an explicit prompt instruction and a human reviewer." The prompt and review layers are real protection; the n-gram check is theatre that takes engineering time to build, test, and maintain. Removing it eliminates a failure mode where a borderline output passes the check and gets shipped because the check "approved" it.

**Rejected:** Keeping n-gram as defense in depth (false confidence has negative value when it influences review behavior). Building real semantic similarity (overkill for v1; revisit when there's evidence the prompt + review layers fail).

---

## D21 — Design hardening from system-design review

**Decision:** Four concrete changes surfaced by a formal system-design pass before implementation. Bundled together because they're closely related edge-case hardening rather than architectural shifts.

### D21a: Pool pick uses SELECT ... FOR UPDATE SKIP LOCKED

When picking a pool row (either for scheduling or for manual operations), wrap the query with `FOR UPDATE SKIP LOCKED`. Prevents two concurrent processes (e.g. the 6h cron firing while `/admin/generate` is running) from racing to claim the same row.

Example:

```sql
SELECT * FROM pool
WHERE status = 'approved'
ORDER BY ...
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

Standard Postgres pattern. Zero operational overhead. Prevents a failure mode that's rare but ugly when it hits.

### D21b: Variety picker handles empty/short history

The variety logic ("avoid regions/eras from the last 3 delivered facts") must gracefully handle days 1-3 when `facts` has fewer than 3 rows. The filter becomes a preference, not a requirement. If applying the filter leaves zero candidates, fall back to oldest approved row.

Concretely: `recent = facts.query().order_by(scheduled_date.desc()).limit(3).all()` — `recent` may be empty or have 1-2 entries; the picker must handle all cases, not just the happy path of 3+.

Launch day is the worst possible moment for a silent crash here.

### D21c: /today cache key includes the date

The in-memory cache key must be today's ISO date, not a constant `"today"`. This ensures the cache auto-invalidates at midnight — no stale window where users get yesterday's fact just after the date rolls over.

Additionally, any write to `facts` (retract, admin schedule) should bust the cache entry for the affected date. Small helper: `cache.pop(date, None)` after any mutation.

### D21d: Retract is "no new views," not "recall"

**Important semantic clarification:** `POST /admin/retract/{date}` prevents future reads via `/today` and `/archive`. It does NOT recall facts already pushed to devices. Users who received the FCM notification still have the fact in Room and will still see it in their local archive.

This is a deliberate limitation, not a bug. True recall would require:
- Server-side registry of which devices received which facts (we explicitly don't maintain this — D17 chose topics over tokens)
- A "recall" FCM push that Android interprets as a delete instruction
- Handling users who were offline when the original push landed but come online after recall

None of that is built. For v1, retract is best-effort cleanup of a mistake going forward. If recall ever matters (legal takedown, something genuinely offensive slipped through review), the only options are (a) push a follow-up notification acknowledging the error or (b) ship an app update that filters the specific fact client-side. Both are manual processes, not architectural features.

**Why bundled:** All four are small, hardening-focused, and discovered in the same review pass. Separate decisions would fragment the rationale. Any one of them alone isn't architecturally significant enough to warrant its own entry.

**Rejected:** Building a full recall system (massive complexity for an unproven need). Semantic uniqueness checks beyond date-based cache keys (overkill). Distributed locking via Redis or similar (`FOR UPDATE SKIP LOCKED` is the Postgres-native answer; adding Redis violates our "stay simple" principle).

---

## D22 — Flutter as unified mobile codebase, iOS as Phase 2.5

**Decision:** Phase 2 is rewritten as a Flutter (Dart) codebase targeting both Android and iOS from a single source. The existing Android Kotlin/Jetpack Compose prototype becomes archived reference material — patterns reused, code rewritten. iOS launches as a Phase 2.5 milestone after Android ships, sharing the same Flutter codebase.

**Why:** Will's parents are iOS users and asked for the app. Two native codebases is unsustainable for a solo dev with Claude Code as multiplier — every feature change ports twice, every bug diagnosed twice, OEM quirks doubled. Flutter genuinely fits this app's requirements (typography- and spacing-driven UI, no complex animations, one feed screen + one settings screen). FCM topic-based push (D17) already made the backend iOS-ready by accident — no backend changes needed for iOS support beyond a small payload tweak in Step 9. The original Android prototype was already ≈70% slated for deletion in Phase 2 (Gemini path removal, entity refactor, WorkManager replacement), so the rewrite cost is similar to the planned refactor.

**Why iOS as Phase 2.5 (not now, not as Phase 2):** Android approval is more forgiving and gives launch experience before tackling Apple's stricter review process. iOS submission benefits from lessons learned. Apple Developer account ($99/yr) deferred until iOS work actually starts.

**Rejected:** Native iOS as a second codebase (the exact problem we're avoiding). React Native (community fragmentation, less polish than Flutter for this UI scope). PWA (iOS PWA push notifications are second-class, breaks the entire product premise). Staying Android-only (loses parents' actual user base, signals lack of commitment).

---

## D23 — LLM-as-judge with calibration set, modifies D9

**Decision:** Replace pure-human review with a judge-triaged auto-approval system. Each generated fact is scored by a separate LLM call (the "judge") that auto-approves HIGH-quality facts, auto-rejects LOW-quality facts, and routes BORDERLINE facts to the existing human review UI. The judge is calibrated against a manually-rated training set of 150-200 facts using few-shot prompting + agreement metric.

**Why:** D9's pure-human review has a real bus-factor problem — if Will is unavailable for >7 days the approved pool drains and the app silently dies. But pure auto-approval has same-model bias risks (Gemini judging Gemini misses the same blind spots), style drift becomes invisible, and tonal misjudgments slip through. The middle path — triage with auto-approval at high confidence and human review at borderline — gets most of the volume autopilot while keeping Will in the loop on edge cases. Holiday-proof by design.

**Implementation:**
- New columns on `pool`: `judge_score` (numeric), `judge_verdict` (`approved` | `borderline` | `rejected_by_judge`)
- Judge call happens at generation time (one call per fact, immutable thereafter)
- Threshold values configurable (start strict, loosen as confidence grows)
- Existing review UI from Step 8 unchanged — just receives fewer items
- Calibration: generate 200 facts (Step 13), Will rates manually via review UI over ≈1 week, judge prompt iterates against rated set until agreement >85% on held-out subset (Step 14)

**Why few-shot, not fine-tuning:** Dataset too small (200 examples is below the floor where fine-tuning beats few-shot on a frontier model). Iteration cycle on prompt is minutes; on fine-tune is days. Frontier models reason over examples rather than learning hard boundaries from them — better fit for fuzzy taste-based judgment. Avoids fine-tuning infrastructure, recurring cost, and lock-in to a specific model version.

**Rejected:** Pure human review (bus factor of 1, holiday risk). Pure auto-approval (same-model bias, invisible drift, tonal misjudgments). Fine-tuned classifier (wrong tool at this scale).

---

## D24 — Pure themed Material 3 for unified mobile UI

**Decision:** Both Android and iOS Flutter app use Material 3 design system, themed only at the surface level (colors, fonts, shapes). All standard Material widgets — no custom widgets, no adaptive iOS-native variants.

**Why:** HistoryBites' UI is fundamentally typography- and spacing-driven — there's no place where a Material switch vs a Cupertino switch genuinely changes user experience. Adaptive design doubles the widget testing surface for marginal aesthetic gain. Custom widgets (Level 2) defer ship date by ≈3-4 days for a v1 where shipping faster matters more than design refinement. Material 3 looks genuinely good on iOS now (used by Google's own iOS apps and many indies). Single design system means a single mental model when iterating.

**Tradeoff:** App will feel slightly less "iOS-native" than a fully adaptive design. Mitigation: lean into editorial feel via typography (serif body type for fact text, generous whitespace) so the differentiation comes from content presentation, not platform chrome. If Material 3 proves limiting in practice, can revisit Level 2 (themed + custom FactCard) as Phase 3 polish work.

**Rejected:** Adaptive design (Material on Android, Cupertino on iOS — doubles testing surface, marginal user-visible benefit). Level 2 themed + custom widgets (deferred until evidence Material 3 is limiting). Level 3 full custom design system (tar pit, weeks-to-months of work, almost always a mistake for solo dev).

---

## D25 — Growth-track maintenance posture with graceful wind-down protocol

**Decision:** HistoryBites is operated as a growth-track project — intent is to grow the app and eventually monetize — with a high-attention launch period followed by a sustainable steady-state cadence. A graceful wind-down protocol is documented in advance for the case where maintenance becomes unsustainable.

**Why:** Will's stated ambition is growth + eventual monetization. Will's stated capacity is "whatever it takes during launch, then taper." This combination requires explicit planning. Most ambitious side projects fail not from lack of features but from a missing transition from launch energy to sustainable maintenance. The Maintenance Playbook documents the cadence; this decision codifies the commitment so future-Will (and Claude Code) understands why operational choices are made the way they are.

**Implications for architectural choices:**
- Bias toward operational simplicity (already done): single FastAPI service, single Postgres, no microservices. Easier to maintain solo, easier to hand off, easier to wind down.
- Substitutable dependencies: mainstream packages, no obscure tooling. Avoids lock-in that would make a future hand-off or wind-down painful.
- DECISIONS.md mirror in repo (D19): ensures architectural rationale survives even if Notion access is lost — critical for both wind-down and hand-off.
- Stdlib-first (D11): minimizes the load on future maintainers (or future-Will after a 6-month break).
- Documented wind-down protocol: turns "the app dies" into "the app retires gracefully" if maintenance lapses. Protects the user experience and the project's legacy.

**Rejected:**
- Hobby-only posture — would have skipped growth investments (ASO, marketing, monetization roadmap), no longer aligned with stated ambition
- Sustained launch-pace forever — unsustainable for a Master's student with a day job, risks burnout, and burnout-driven shutdown is the ungraceful kind
- No wind-down plan — leaves the project to die badly if maintenance lapses, which is bad for users and bad for Will's track record

---

## D26 — 5-point Likert rating as primary review label, status derived

**Decision:** Replace the binary approve/reject action on `POST /admin/review/{pool_id}` with a 1–5 ordinal rating (`review_rating SMALLINT NOT NULL`, CHECK 1–5). The `pool.status` field is no longer set directly by the operator — it derives from rating: `rating >= 4 → 'approved'`, `rating <= 3 → 'rejected'`. Tags (D-implementation 13a) and notes stay as-is.

**Why:** During the 13b rating sprint, Will hit cases where binary forced choice corrupted the calibration signal — "I'll put the explanation why I like it but reject it anyways" and vice versa. Tag-and-note signal disagreed with action signal. The judge in D23 trains on `(features, label)` pairs; with binary labels, a confident-yes and a barely-yes look identical, and contradictory tag/action pairs teach the wrong lesson. A 1–5 ordinal label transmits *strength* of judgment, lets the judge become an ordinal regressor instead of a binary classifier, and makes auto-decision thresholds (e.g. predicted rating ≥ 4.2) statistically meaningful instead of probabilistic guesses. Re-rating allowed (drop the once-only guard) because calibration drift is real and revisiting earlier judgments is part of the process.

**Why 5 points specifically:** 5 is the standard Likert cardinality for ordinal judgments. 3 forces too many decisions through a single "medium" anchor; 7 introduces noise without proportional signal. 5 gives a clear middle (3 = borderline), two confident extremes (1, 5), and two adjacent levels (2, 4) where the operator can express directional lean without forced commitment.

**Why rated, not tag-rated:** Considered per-tag 5-point ratings (4–5 tags × 5 points × 150 facts = 600–750 micro-judgments). Rejected: tag ratings are highly correlated with overall rating, the marginal information is small, and the cognitive load risks pushing all ratings toward the 3-anchor under fatigue. Simpler to capture overall rating well and let the judge learn tag-attribution patterns from the rating + tags combination.

**Threshold (4 vs 3):** Threshold is 4 (rating ≥ 4 → approved). Rating 3 means "borderline / I'm not sure" — pinning that to approved would let lukewarm content reach users; pinning it to rejected discards the operator's hesitation. Treating 3 as rejected is the safer default for a daily-fact app where a published miss costs more than an unpublished hit.

**Existing 39 rated rows (pre-D26):** Re-rate, don't backfill. Mechanical mapping (`approved → 4`, `rejected → 2`) is lossy — the binary labels were already noisy per Will's own observation. Re-rating is ≈10 minutes; backfilling permanently corrupts the calibration set.

**Rejected:** Binary status as primary signal (the cause of the calibration drift). Per-tag rating (cognitive overhead with diminishing returns). 7-point or 10-point scales (more noise than signal). Threshold = 3 (lets borderline reach users).

---

## D27 — pool uniqueness widened to (source_name, external_id, prompt_version) for multi-version A/B testing

**Decision:** The unique constraint on `pool` is widened from `(source_name, external_id)` to `(source_name, external_id, prompt_version)`. The same Wikipedia article can now have at most one row *per prompt version*, instead of at most one row total. Topic-level dedup within a single prompt version (D3) is unchanged — still enforced application-side via `get_used_external_ids` in the generation pipeline.

**Why:** Step 13d introduces a v2 prompt and runs a targeted A/B regeneration against the 65-row "boring-even-if-true" cohort from the v1 calibration set. The point of the A/B is to compare v1 vs v2 outputs *on the same source articles* — head-to-head on identical inputs is the only way to attribute rating differences to the prompt rather than to article variance. The old constraint blocked that: inserting a v2 row with the same `(source_name, external_id)` as the existing v1 row hit the unique key and failed. Widening unblocks the A/B without giving up the safety net entirely; same-version duplicates on the same article are still rejected.

**Why constraint, not just app-side check:** The constraint is a race-condition backstop, not the primary dedup mechanism. Two concurrent generation calls hitting the same `(source_name, external_id, prompt_version)` would both pass the application-side `get_used_external_ids` check and the constraint would catch the loser at INSERT time (`IntegrityError` -> rollback -> next candidate). Removing the constraint entirely would lose that backstop and make duplicate same-version rows possible under load.

**Why include prompt_version, not model_used:** `prompt_version` is the variable we're A/B-ing. `model_used` (e.g. `"gemini:gemini-2.5-flash"`) varies across providers and over time as Google ships new model revisions, but it's not a deliberate experimental dimension — facts generated by `gemini-2.5-flash-001` and `gemini-2.5-flash-002` against the same prompt are conceptually the same v1 output. If we ever want to A/B *models* on the same article, that becomes its own decision.

**Implications:**
- The pool can grow ~2× when running an A/B regeneration (v1 + v2 rows on the same articles). Storage cost is trivial (~150 rows × ~1KB per row = ~150KB extra).
- The variety scorer in D21b reads `region` and `era` off `pool` rows and doesn't care about `prompt_version` — variety semantics unchanged.
- The scheduler in D21a/b promotes any `approved` row regardless of `prompt_version`; if both v1 and v2 versions of the same article get approved, either could be picked. Acceptable: by the time v2 is shipped (Step 14+), the cron will be flipped to `PROMPT_VERSION=v2` and only v2 rows will be generated going forward, so the overlap window is bounded.
- Downgrade is destructive — rolling back this migration on a database that contains cross-version duplicates will fail. Intended: silently dropping half the calibration set on a downgrade is worse than a noisy migration error.

**Rejected:**
- Adding `prompt_version` as a column on the existing constraint via `ALTER TABLE ... ADD COLUMN TO INDEX` (Postgres doesn't support that directly; drop + recreate is the standard pattern, which is what this migration does).
- Removing the constraint entirely and relying solely on app-side dedup (loses the race-condition backstop).
- Including `model_used` in the constraint key (not the experimental dimension; makes future model upgrades silently introduce duplicate rows).
- Soft-deleting the v1 row on v2 generation (loses the calibration baseline; defeats the entire point of the A/B).
- A separate `pool_v2` table (architectural duplication; every consumer — admin endpoints, scheduler, /health counts — would need to learn about both tables).

---

## D28 — Code Review Pre-Phase-2 architectural outcomes

**Decision:** Six concrete architectural changes from the Code Review Pre-Phase-2 chain (Fix 1 → Fix 6 + Cron Architecture Fix A→E). Bundled because they're outcomes of one coordinated review process; separate decisions would fragment the rationale.

### D28a: Cookie-based admin handoff (Fix 6, reverses Fix 1's deferral)

The admin browser flow uses an HttpOnly + Secure + SameSite=Strict session cookie set by `POST /admin/login`, replacing Fix 1's `?token=...` query-string approach. Cookie is `Path=/admin`-scoped with a 30-day default lifetime. Bearer-header auth (curl, Phase 2 mobile) is unchanged.

**Why:** Tokens in URLs leak (browser history, server logs, referrer headers). The cookie path keeps the same UX (paste token once, browse review queue) without the leakage. SameSite=Strict + Path=/admin + HttpOnly contain the cookie tightly. Reverses Fix 1's "defer cookie auth" call from earlier in the review chain — Fix 1 deemed cookies overkill for solo-operator review; production exposure (token in copy-pasted URL → browser history persistence + screenshot risk) made the cost/benefit flip.

### D28b: Auth-channel collapse from three to two (header + cookie)

Pre-Fix-1 admin auth accepted three sources: `Authorization: Bearer`, `?token=...` query string, and a hidden form field. Fix 1 narrowed to two-with-strict-routing. Fix 6 collapsed back to a single unified `verify_admin_token` dependency that accepts header OR cookie OR form, with the query path eliminated entirely.

**Why:** Fewer code paths means a single 401 surface for OpenAPI consistency and removes the URL-leakage attack vector. Form path stays for the `/admin/login` bootstrap. Single dep applied at router level (`dependencies=[Depends(verify_admin_token)]`) inherits to every nested route.

### D28c: `ErrorDetail` canonical error envelope (Fix 4 P2.2)

All admin endpoints declare a 401 response with `model=ErrorDetail` for OpenAPI consistency (`responses={401: {"model": ErrorDetail}}` at router level). ErrorDetail is the standard `{detail: str}` shape FastAPI uses by default; declaring it explicitly stops the OpenAPI spec from emitting each route's 401 with a different ad-hoc body.

**Why:** Phase 2 Flutter clients generated from OpenAPI need a single error type, not N variants. Single declaration at router level propagates correctly through Cleanup-A's admin-gated `/openapi.json` once Fix 6's cookie path is live.

### D28d: JSONFormatter traceback rendering (Fix 3 P2.1)

The custom JSONFormatter at `app/main.py` renders `record.exc_info` into `exc_type`, `exc_message`, and `traceback` fields when present. Without this, `logger.exception(...)` calls silently dropped tracebacks despite the call site explicitly asking for them.

**Why:** `admin.py` admin_run_generation broad-except (line 725) and `cron.py` CLI catch-all (line 344) both relied on `logger.exception` for production diagnostics — pre-fix neither produced a stack trace in Railway logs. Catch-all except blocks without tracebacks are invisible failures; the cost was log-noise diagnosis vs zero diagnosis. Clearly worth it.

### D28e: FastAPI lifespan migration (Fix 4 P3.3)

Migrated from FastAPI's deprecated `@app.on_event("startup")` / `@app.on_event("shutdown")` to the modern `lifespan=` async context manager. Wikipedia `httpx.AsyncClient` opens pre-yield and closes post-yield via `await wikipedia.aclose()`.

**Why:** `on_event` has been deprecated since FastAPI 0.93 (2023). Lifespan handlers also let us share state via context and don't depend on event timing — cleaner semantics, future-proof for FastAPI's eventual removal of the legacy hooks. Note: cron CLI path doesn't go through lifespan (Session A.2 observation), but Session C.10 verified Wikipedia client exit is observed-clean within ~15–20s of last DB write (well under Railway's 30s container-teardown budget). Belt-and-braces explicit close in the cron CLI is a deferred nice-to-have.

### D28f: Per-service Config-as-Code paths (Cron Architecture Fix chain)

Each Railway service points at its own `railway.*.toml`: API → `/railway.toml`, push-cron → `/railway.push-cron.toml`, gen-cron → `/railway.generation-cron.toml`. Per-service files only specify fields that differ from `/railway.toml`; the rest fall back to defaults (Backend Architecture cron footnote 4).

**Why:** Railway's `[deploy] startCommand` from `/railway.toml` overrides any dashboard-set Custom Start Command (Push Forensics 2 diagnostic — Hypothesis B confirmed). Without per-service config, every service bound to the repo runs the API's start command (uvicorn) instead of its own (`python -m app.cron run_push` etc.). Per-service config files give each service its own `startCommand` while sharing the base build config. The decorative `[[cron]]` blocks that previously sat in `/railway.toml` are stripped — Railway doesn't honor per-cron `command` fields; the schedule lives on each service's dashboard.

**Implication:** Every push to main auto-fans-out to all 3 services bound to that branch (Backend Architecture cron footnote 1). For scope-to-one-service deploys, use `railway up -s <svc>` from a non-default branch.

**Why bundled:** All six are outcomes of the Code Review Pre-Phase-2 chain (Fix 1 → Fix 6 + Cron Architecture Fix A→E). Architecturally they touch different layers (auth UX, error envelope, observability, lifecycle, deploy topology), but they shipped as one coordinated hardening pass with a single deploy lifecycle. Separate D28–D33 entries would fragment rationale that's better understood as a unit — the narrative arc (Code Review surfaces issues → Fixes 1–6 → cron architecture forensics → per-service config) is the load-bearing context.

**Rejected:**
- Separate D28–D33 entries (one per item) — fragments rationale; loses the chain's narrative arc
- Folding Cleanup-A's Codex P2 hardening (security headers, OpenAPI gating, deps pinning) into D28 — those came from Codex's independent audit, not the Pre-Phase-2 chain. A future D29+ may capture Codex outcomes if any rise to architectural-decision level
- Verbatim from PR descriptions — too narrow; D28 captures the *why* once for posterity, not the per-commit changelog

