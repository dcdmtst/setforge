"""Adapters. The core never depends on a model vendor's response objects."""

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from .io import inside, read, write


def redacted(text):
    for key, value in os.environ.items():
        if len(value) >= 8 and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    return text


def execute(argv, cwd, timeout, job, stdin=None):
    try:
        result = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Provider timed out after {timeout}s; attempt remains recorded.") from exc
    (job / "provider.stderr.txt").write_text(redacted(result.stderr), encoding="utf-8")
    if result.returncode:
        raise ValueError(f"Provider exit {result.returncode}; see provider.stderr.txt.")
    return result.stdout


def invoke(config, request, run, job, timeout):
    kind = config["type"]
    if kind == "files":
        return None
    images = request["images"]
    if images and config.get("supports_images", True) is False:
        raise ValueError("Provider does not support the image inputs required by this task.")
    if len(images) > config.get("max_images", 32):
        raise ValueError("Provider image limit is too low; references will not be silently dropped.")
    req_path = job / "request.json"
    response_path = job / "worker-response.json"
    if kind == "command":
        substitutions = {"request": str(req_path), "response": str(response_path), "python": sys.executable}
        argv = [arg.format_map(substitutions) for arg in config["argv"]]
        output = execute(argv, run, timeout, job, json.dumps(request, ensure_ascii=False))
        return read(response_path) if response_path.exists() else json.loads(output)
    if kind == "codex":
        binary = config.get("executable") or shutil.which("codex.cmd" if os.name == "nt" else "codex")
        if not binary:
            raise ValueError("Codex CLI is not installed; use files or another provider.")
        argv = [binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                "--output-schema", str(job / "schema.json"), "-o", str(response_path)]
        if config.get("model"):
            argv += ["--model", config["model"]]
        for image in images:
            argv += ["--image", str(inside(run, image["path"]))]
        argv += ["-"]
        # Do not put an unbounded art-production agent behind the CLI: this adapter
        # is restricted to reasoning/review and returns a validated data object.
        execute(argv, run, timeout, job,
                "Return only the requested JSON. Do not change files or run production tools.\n"
                + json.dumps(request, ensure_ascii=False))
        return read(response_path)
    if kind == "chat-completions":
        key = os.environ.get(config.get("key_env", "OPENAI_API_KEY"))
        if not key and config.get("require_key", True):
            raise ValueError("Provider credential environment variable is not set.")
        endpoint = config["base_url"].rstrip("/") + "/chat/completions"
        if not endpoint.startswith("https://") and not endpoint.startswith(("http://localhost:", "http://127.0.0.1:")):
            raise ValueError("API endpoint must use HTTPS or a local development server.")
        content = [{"type": "text", "text": json.dumps(request, ensure_ascii=False)}]
        for image in images:
            path = inside(run, image["path"])
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()}})
        fmt = {"type": "json_schema", "json_schema": {
            "name": "setforge_output", "strict": True, "schema": request["output_schema"]}}
        if config.get("json_schema", True) is False:
            fmt = {"type": "json_object"}
        payload = {"model": config["model"], "messages": [{"role": "user", "content": content}],
                   "response_format": fmt}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = Request(endpoint, json.dumps(payload).encode(), headers)
        with urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        write(job / "usage.json", data.get("usage", {}))
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("Text API returned no choices.")
        message = choices[0].get("message", {})
        if message.get("refusal"):
            raise ValueError("Text provider refused this request; inspect its inputs.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Text API returned no JSON content.")
        return json.loads(content)
    if kind == "image-cli":
        from .image_api import invoke_image_api
        return invoke_image_api(config,request,run,job,timeout)
    raise ValueError(f"Unsupported provider: {kind}")
