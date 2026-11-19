# Setforge agent contract

For installation use SETUP_AGENT.md. For production read the project-scoped
`.agents/skills/collection-set-pipeline/SKILL.md` and docs/parallel-workers.md.
This contract also works for agents without Codex skill support.

The repository is public to read; only its owner `@dcdmtst` has write access.
The owner may push directly or use a PR; neither a PR nor a separate approval is
mandatory. External forks and PRs do not grant write access to this repository.
Do not grant collaborators, invitees or deploy keys write access without an
explicit owner instruction. See docs/contributing.md.

- Inputs: theme (required), free-form wishes (optional, preserve verbatim),
  variants (3–5, default 3). Output **all requested variants**, ten accepted cards
  per set; do not silently choose one variant or return only a pilot.
- Use the original user-supplied reference pack; inspect it visually. Never use
  generated revisions as substitutes for original style evidence. Do not reuse
  constraints from a different brief.
- Before art, review concepts for distinctness and interest, assign all four
  categories to each set, stars, gold and cast, and pass English title pixel-fit
  QA. Characters, gold and stars are separate decisions. No invented drop rates.
- Choose the requested font before title QA (default Lithos Pro Black). If absent,
  ask install or default Aileron; docs/typography.md has OS-specific instructions
  and explicit auto-install. CI may use --missing-font default. Persist the actual
  font per run; a missing saved font falls back with a notice and fresh fit QA.
  Font software stays outside this repository and producer ZIPs.
- Preserve intentional recurring entities through explicit scoped canon.
  Qualifying a character does not accept its source card or its defective props.
- Use the persisted CLI jobs, leases and exact request contracts. Only the
  coordinator advances state. Never write state/acceptance/queue database by hand.
- Default team: coordinator + chief reviewer + two flex artists. Use independent
  subagents/worker identities when the environment provides them. Increase
  `--artists`, `--reviewers`, `--workers` to fit a larger environment. Do not
  invent separate identities for the same worker. No author may review any
  version of their card, entity source, or containing set. Without independent
  reviewers, obtain another executor or human review; do not claim acceptance.
- Review actual images at native and thumbnail size. Q95 ≥ 93.5 **unrounded**,
  nine dimensions and all hard gates. Up to 3 edits after iteration 0. Plateau
  changes correction strategy. At the cap discard a failing card; the incomplete
  set fails. Continue unrelated jobs; stop below threshold is never a pass.
- Before scores, audit physical construction: whole/parts, joints, lid or drawer
  travel/closed fit, support and actor contact. New jobs require exact-pixel
  construction_review; unconvincing essential assembly blocks acceptance regardless
  of Q95. Final-set review rechecks risky constructions. See docs/art-acceptance.md.
- Explicit post-delivery corrections use revise-card, never manual state edits.
  Preserve original judgements/PNGs and spent attempts; reopen only the target.
- Default image execution is the active agent's **builtin imagegen**, via files
  mode. Do not launch a nested `codex exec` and assume it has your image tools.
  Do not invent unavailable API wrappers or silently switch to paid access.
- Use docs/image-execution.md: prepare-job selects at most 5 total imagegen inputs,
  preserving edit target/canon and recording omitted original references. Inspect
  full inputs; send only selected paths. Wrap one tool call in image-start and
  image-finish. The CLI writes timestamps, hashes and response JSON; do not
  hand-transcribe metadata or silently retry an already recorded attempt.
- Additional cash budget is 0 by default. Capability checks generate nothing.
  Optional generator probes need explicit user request/approval and zero-cash
  tools. Credentials stay in environment variables and out of commands/logs.
- Finish with `coordinate`, `result-manifest`, `export`. A complete producer ZIP
  is separate from the private full-history `pack`. Display report and ZIP links.
  Failed orders have an honest manifest and report, not a supposedly accepted ZIP.
- Runtime `.setforge/`, work, real art, references, requests, reports and local
  configuration never enter source control. Run `scripts/publication_check.py`
  before staging/publishing. No network publication without a user instruction.

For changes to this release, preserve the modules listed in BASELINE.json unless
the user explicitly requests an engine change. Do not add dependencies on private
workspace paths, an account, Miro, GitHub, or one particular agent runtime.
