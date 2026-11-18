# Install and open Setforge for the user

Read AGENTS.md. Work inside this checkout. Do not change global agent configuration,
credentials or the user's other projects. Do not call a model during installation.

## Obtain the checkout

The human's only initial action is to give their agent the README installation
text. Do not ask them to clone, unzip, create a manifest, or run a shell command.

If the checkout has not yet been obtained, first ask where to install it and wait
for the answer. Source: https://github.com/dcdmtst/setforge. Clone into the chosen
folder; if Git is unavailable, download
https://github.com/dcdmtst/setforge/archive/refs/heads/main.zip and extract the
repository root there. Do not overwrite an existing folder's files or create a
second installation inside the current checkout. If it is already installed,
use that checkout; resolve an occupied destination by asking whether to use it or
choose a different folder. Then read this file from the actual checkout and
continue below. Downloading code does not supply an image generator or references.

## Install, connect references and open the page

1. Find Python 3.11+ (`python`, `python3`, or `py -3`). If absent, install it
   from an official source when the environment permits; explain a concrete
   access/permission blocker if you cannot. Do not send the human a list of
   terminal commands to execute on your behalf. Run
   `python scripts/setup.py` with the appropriate Python launcher. This installs
   the local package into `.venv`; no global pip install is needed.
2. Check your **actual tool inventory**. Prefer builtin imagegen with generation,
   editing and image-reference support, paid for by the user's existing access.
   Do not equate an installed Codex binary or an API key with builtin tool access.
   If the tool is unavailable, say so; use only an explicitly authorised free
   alternative. Optional discovery/probe is described in docs/generators.md.
3. Ask which images to use as references and where to obtain them: a local folder,
   attached files or another source explicitly supplied by the user. Ask for style
   wishes only if they have not already been provided. Do not require a prepared
   style pack. Follow docs/reference-pack.md yourself: inspect the supplied images,
   assemble style-pack.json, write style.md and create the reference catalogue in
   a separate local data folder. Reuse a valid supplied pack if one already exists.
   Describe uncertain image interpretations honestly; only ask the user about
   choices that materially change their intended style. The repository and skill
   do NOT include the original 160 images or a trained style model. Do not fetch
   proprietary references from another workspace or publish them. An empty project
   can serve the setup page but cannot produce valid art; do not claim setup is
   production-ready until real references are connected.
4. Import it using `.venv` Python:
   `python -m setforge import-style --project .setforge/project --style-pack PACK`.
   Here and below, `python` means `.venv/Scripts/python.exe` on Windows or
   `.venv/bin/python` on macOS/Linux, not a global interpreter.
5. Start the page in a background process with stdout/stderr redirected to
   `.setforge/server.log`. The installation helper supports this directly:
   `SYSTEM_PYTHON scripts/setup.py --start --open`.
   On Windows it creates no visible console window. It never kills a process
   already using the port; choose `--port ANOTHER_PORT` if necessary.
6. Verify GET `/` returns the producer form; GET `/api/context` reports `ready:true`.
   Run `python -m setforge doctor --project .setforge/project`. Tell the user the
   local page URL and whether model access is verified or still missing.
7. Stay active to consume new orders with `inbox --runs .setforge/runs
   --wait-seconds 30`, then follow AGENTS.md. Use the same interpreter for every
   command. Do not end the turn claiming an unattended service exists when only
   this chat has imagegen. If the user wants you to stop waiting, stop waiting;
   submitted files remain available to resume later.

## Production policy

Acceptance requires **unrounded Q95 >= 93.5 and every mandatory gate**: physical
construction, technical quality, reference style, class/category and canon.
Allow at most three edits after the initial image. Plateau changes correction
strategy; stopping below the threshold is never acceptance. An independent
reviewer inspects the native and thumbnail; an author never accepts their own
card. Follow docs/art-acceptance.md and the exact saved policy/schema of each run.
The copyable human installation prompt does not configure or override this policy.

For production read docs/image-execution.md before dispatching the artists.
Builtin imagegen accepts at most 5 total inputs, including edit target and canon.
Use prepare-job's image_call selection; the full original input list stays available
for inspection/review. Never hand-trim identities to make a request fit.
Use image-start immediately before one tool call, image-finish immediately after
with its actual PNG path (or --error). The CLI writes correct timestamps, duration,
SHA256 and image-response.json automatically. Do not ask the producer to repair
metadata or hand-write generation-record.json. Record unavailable capabilities
honestly; these helpers do not supply a missing image model or revive an idle chat.

Future reference/style changes are documented in docs/reference-pack.md. Do not
replace references, recalibrate scores or run a style experiment during installation
unless the user requests it. For a different style, prefer a separate project so
the old collection canon does not leak into it.

The server is local-only (`127.0.0.1`). Closing the browser does not stop its
background process. `.setforge/server.json` records its PID and command; stop
only that verified process when requested. Foreground alternative:
`python -m setforge ui --project .setforge/project --runs .setforge/runs` (Ctrl+C).

Do not run real production as an installation smoke test. The user creates the
first brief in the page. All requested 3–5 variants must remain visible and be
processed, with per-card retries and final set review. After a complete order:

```text
python -m setforge result-manifest --run RUN
python -m setforge export --run RUN
```

Verify the manifest says `full_delivery_ready:true`, open `RUN/report.html`,
and check the accepted PNGs and `delivery.zip`. Report partial/rejected work
honestly. Never rewrite scores, remove a failing card from the requested count,
or add budget to get a green result.
