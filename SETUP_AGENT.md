# Install and open Setforge for the user

Read AGENTS.md. Work inside this checkout. Do not change global agent configuration,
credentials or the user's other projects. Do not call a model during installation.

1. Find Python 3.11+ (`python`, `python3`, or `py -3`). Run
   `python scripts/setup.py` with the appropriate Python launcher. This installs
   the local package into `.venv`; no global pip install is needed.
2. Check your **actual tool inventory**. Prefer builtin imagegen with generation,
   editing and image-reference support, paid for by the user's existing access.
   Do not equate an installed Codex binary or an API key with builtin tool access.
   If the tool is unavailable, say so; use only an explicitly authorised free
   alternative. Optional discovery/probe is described in docs/generators.md.
3. Obtain a local style pack described in docs/reference-pack.md. The repository
   and skill do NOT include the original160 images or a trained style model.
   To reproduce that look the user must supply the original catalogue locally;
   otherwise use their own authorised reference pack and its style.md. Search the
   user-specified local folder first. Do not fetch proprietary references from
   another workspace or publish them. If absent, ask for the folder once. An
   empty project can still serve the setup page; it cannot produce valid art.
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

For production read docs/image-execution.md before dispatching the artists.
Builtin imagegen accepts at most5 total inputs, including edit target and canon.
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
