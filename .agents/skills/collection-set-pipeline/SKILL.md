---
name: collection-set-pipeline
description: Set up Setforge, connect reference images, start a project with a new style, and produce or independently review 3–5 collectible-card sets of ten illustrations from a theme and producer wishes. Use for Setforge setup, style projects and new or continuing orders.
---

# Collection set pipeline

Read AGENTS.md and locate the project/run. For installation and the local web
page read SETUP_AGENT.md. For agents without skills these files are ordinary
instructions. Use `.venv` Python and `python -m setforge` if setforge is not on PATH.
Do not inherit a different brief's character/gold constraints.

## Human commands for setup and style

Treat these phrases as requests to do the work, not instructions the human must
translate into CLI commands. Follow SETUP_AGENT.md and docs/reference-pack.md.

- **«Установи и запусти Setforge»**: ask where to install, obtain
  https://github.com/dcdmtst/setforge (Git or main.zip), then execute SETUP_AGENT.md.
  Connect references, start the local page and return its URL. The human need not
  download, unzip or prepare configuration files.
- **«Подключи мои эталоны»**: ask for the images/source and optional style wishes,
  prepare the pack/catalogue/style.md yourself, then connect them for future
  orders. Resolve the installed project path if unknown. Keep existing orders.
- **«Создай новый проект с новым стилем»**: ask where to create the separate
  project, which images to use and any style wishes. Prepare a new pack and guide,
  create a fresh project and runs folder, start its local page and return the URL.
  Keep old references, canon, orders and servers intact; use another free port.
  If Setforge is absent, complete installation first. Do not generate art or run
  a style benchmark just to create the project. See the named section in
  docs/reference-pack.md for the executable steps.

## Start and coordinate

`run --project PROJECT --brief BRIEF --run NEW_RUN` creates all requested variants
(default 3, allowed 3–5), ten cards each. Free-form wishes are preserved verbatim.
Use `inbox --runs RUNS --wait-seconds 30` for orders created in the local page.
The files adapter requires an active agent; a completed chat will not wake itself.

Default team: coordinator, chief reviewer, two flex artists. Use independent
subagents/worker executors when available. Default capacities: `--artists 2
--reviewers 2 --workers 3`; coordinator is outside the worker cap. Increase them
for a larger environment. One physical executor keeps one worker ID across roles.
Only coordinator runs `coordinate --run RUN` and changes shared state/canon.
Only chief does full-plan and full-set review. A worker cannot review ANY version
of a card it generated, its entity source, or a set containing it. Switching aliases
is not independence. If no independent executor exists, obtain one or human review.

Read docs/parallel-workers.md for queue, recovery and timing details.

```text
setforge coordinate --run RUN
setforge claim --run RUN --worker artist-1 --role flex --wait-seconds 5
setforge prepare-job --run RUN --job JOB --worker artist-1
setforge heartbeat --run RUN --job JOB --worker artist-1 --token TOKEN --lease-seconds 900
setforge validate-response --run RUN --job JOB --response ANSWER.json
setforge deliver --run RUN --job JOB --worker artist-1 --token TOKEN --response ANSWER.json --model ACTUAL_MODEL
```

Use the returned exact job ID/token, request.json and schema.json. A chief uses
`--role reviewer`, planner uses `--role planner`. Flex workers may take independent
reviews to limit backlog. Claims/heartbeats do not accept art. Deliver stores the
answer; coordinator applies it. Workers do not edit shared JSON/database or spawn
unbounded artists. Idle for one role does not mean the order is finished.

## Before art

Inspect ORIGINAL user-supplied style references visually. Plan all variants before
art; distinct interpretations, semantic roles and interesting details, not palette
variants. Check the exact wishes at plan, card and set gates. A narrative sequence
is optional; coherent situation/activity/place is enough.

Assign stars 1–5, gold, category, cast and reasons BEFORE images. Default stars are
two of each rank (a project choice, not a claimed reference law). Every set has all
categories 1–4 independent of stars. Category 5 is an additional golden scene with
one/two key characters, not a substitute for 1–4. Ordinary characters can appear
without gold. Character and no-gold policies are independent. Read docs/allocation.md.

English set/card titles only.
Choose the project/order font before art (default Lithos Pro Black). If missing,
ask whether to install or use default Aileron; follow docs/typography.md for the
OS-specific manual/automatic flow. Never purchase a font or call an attempted
Adobe launch a completed installation. For CI use --missing-font default; record
the effective fallback. A missing saved font falls back and title fit is rechecked.
Measure using the actual renderer at fixed size: card 18 px/160 px, set 31 px/850 px.
Never shrink, crop, ellipsize or widen to make a
particular title pass. Rewrite naturally; if impossible replace the concept before
art. Localization and loc QA are out of scope. Read docs/naming-guide.md.

## Image execution

Default is builtin imagegen available to the ACTIVE agent, not nested codex exec
or an assumed API model. Confirm real generate/edit/reference capability. Budget 0
means no extra cash; no silent paid fallback. Read docs/generators.md for optional
capability discovery and a user-requested fair probe. Probe-plan creation makes
no model calls; do not report it as a successful benchmark.

One lease = one tool attempt. Inspect all exact inputs with their roles and hashes:
original style, accepted canon, edit target, candidate, thumbnail. Do not treat a
revision as a new original style reference. `reference-cache` records only actual
visual inspections by this worker; hash presence alone is not an inspection.
For builtin imagegen follow docs/image-execution.md. `prepare-job.image_call`
selects at most FIVE total tool images: edit target and mandatory canon first,
then card-specific original style references, then global originals. Byte-identical
inputs share a slot with recorded role aliases. Inspect the full immutable inputs;
send only the prepared selected paths. The CLI records omissions, never silently
drops identity. Review jobs retain their full inputs and are not capped to five.

Finish input inspection/prompt work BEFORE `image-start --run RUN --job JOB
--worker WORKER --token TOKEN`. Pass its exact prompt/paths to ONE imagegen call,
then immediately `image-finish ... --image ABSOLUTE_TOOL_PNG`. The CLI writes
UTC boundaries, seconds, hashes, tool/worker/model and image-response.json; do not
hand-transcribe those fields. Deliver that response via the existing lease.
On a tool failure use `image-finish ... --error REASON`, then abandon that attempt.
Do not retry a started or closed attempt silently. Use --prompt-file before start
for an augmented exact prompt; --model only for an actually disclosed model.
Timing is a dispatch/return envelope, not pure model compute. Use real saved
output, not a reconstruction. No game UI, labels or stars in the art.
Preserve complete major silhouettes, visual interest and varied backgrounds.

New recurring entities need explicit source_card_id and scope(set/collection).
Prioritize simple full-view sources, then dependent scenes. Entity-only approval
is a separate review bound to exact file/hash and approved/excluded components.
An unrelated prop defect need not block usable canon; an obscured or malformed
entity does. An approved entity NEVER accepts its defective source card.

## Review and feedback

Before scoring, physically trace whole objects, their parts, joints/support and
actor contact. For a lid/door/drawer check its matching opening, axis/travel and
plausible closed position; counting hinges is not proof of a coherent assembly.
New card jobs require exact-pixel construction_review. Inspect risky joints
close up in the native and name that region. If essential construction is
unconvincing, fail geometry with a blocking finding and revise even when Q95
passes. Do not invent hidden supports; allow natural occlusion, stylization and
explicit fantasy. Whole-set jobs independently recheck construction_targets.
See docs/art-acceptance.md; structural JSON validation cannot verify the pixels.

Inspect actual native and thumbnail images against ORIGINAL references and brief.
Use exact schema: nine scores 0–100 in steps of 5 with visible evidence; class,
canon, observed category and all criteria. Weights: technical 20, palette 10,
narrative 10, style 15, background 8, materials 12, composition 10, lighting 5, interest 5.
Q95 = weighted sum / 95. The CLI computes it; do not invent the final score.

Accept only **unrounded Q95 >= 93.5 AND all hard gates**. Glossy fat-looking cloth,
wood, fur or other inappropriate materials are defects even if attractive. Rarity
is not a lower quality bar. Give concrete, localized feedback and preserve already
successful aspects. Recheck regressions. Read docs/art-acceptance.md.

Initial iteration 0, at most THREE edits. Two consecutive edits gaining < 0.5 over
best eligible score signal plateau: change correction strategy, not early pass/stop.
If still failing after edit 3, discard unaccepted; a set missing ten accepted cards
fails. Continue independent jobs. Do not reset counters, silently replan indefinitely,
raise scores or manufacture reviewer identities to finish. Existing snapshots retain
their policy; amendments require explicit producer instruction.

Expired image leases are uncertain: recover existing output first. Do not generate
again until the original operation is confirmed finished without recoverable result.
Only then use reconcile with evidence. Abandon records a failed attempt, never a
hidden retry. See docs/parallel-workers.md.

## Deliver

Coordinate through full-set review. All requested sets remain visible in report.
Run `result-manifest --run RUN`, require full_delivery_ready:true, then
`export --run RUN`. Inspect report.html, result-manifest.json and delivery.zip.
ZIP contains only accepted native/game PNGs, presentation and timing files. Private
full-history pack is separate. Rejected/partial work gets an honest report/manifest,
not an accepted ZIP. Show actual paths/links and unresolved failures.

`canon-publish` is explicit cross-run promotion of accepted scoped identity. Old
runs stay unchanged. Never publish references, outputs, local config or history
unless separately requested. Source-only publication uses scripts/publication_check.py.

For an explicit producer rejection after completion, `revise-card --run RUN
--card VARIANT/CARD --note PRODUCER_CORRECTION` preserves prior PNGs, responses,
attempt counts and a pre-change delivery archive. It reopens only that card and
enables its construction gate. Coordinate, generate/review that correction and
repeat final-set review/export; do not hand-edit accepted state or regenerate
the other cards. The prior accepted judgement is historical evidence, not a
valid fallback after the producer invalidated it.
