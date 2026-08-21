"""Small OpenAI-compatible vision/image adapter.

Credentials are loaded from explicit dictionaries in ``mykey.py`` at call time.
Values are never logged or returned.
"""
import base64
import importlib
import mimetypes
import os
import re
from pathlib import Path


# Vision-capable mykey config names, tried after the requested/current model.
VISION_FALLBACKS = [
    "native_oai_config_aihub1",
    "native_oai_config_aihub2",
    "native_oai_config_aihub3",
    "native_oai_config_fluxionai1",
    "native_oai_config_fluxionai2",
    "native_oai_config_google",
    "native_oai_config_openrouter_vision",
    "native_oai_config_qwen3_ssh",
    "native_oai_config_lfm",
]

_DEFAULT_OCR_PROMPT = ("Extract all readable text exactly; preserve layout where possible. "
                       "Output ONLY the extracted text; no commentary, no explanation.")


def _image_config():
    """Use the explicit image2 config from mykey.py."""
    try:
        cfg = getattr(importlib.import_module("mykey"), "native_oai_config_image2")
    except (ImportError, AttributeError) as e:
        raise RuntimeError("explicit image config native_oai_config_image2 is unavailable") from e
    if not isinstance(cfg, dict) or not cfg.get("apikey"):
        raise RuntimeError("explicit image2 credential is not configured")
    return cfg


def _credential(kind):
    if kind == "image":
        cfg = _image_config()
        return cfg["apikey"], cfg
    raise ValueError(f"unknown credential kind: {kind}")


def _read_image(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return mime, base64.b64encode(p.read_bytes()).decode("ascii")


_PREAMBLE_START = re.compile(
    r"^(The user (wants|would like|is asking|asks)|I (need to|will|am going to|'m going to|'ll|can)|"
    r"Sure|Certainly|Of course|Here (is|'s|are)|Below (is|are)|Following is)\b", re.I)
_PREAMBLE_META = re.compile(r"\b(extract\w*|transcri\w*|image\w*|visible text|certificat\w*|text|read\w*)\b", re.I)


def _strip_preamble(text):
    """Drop leading commentary lines if they are clearly model meta-talk before the extraction."""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        first = lines[idx].strip()
        if not first or not first.isascii() or (idx == 0 and len(first) < 40):
            break
        if _PREAMBLE_START.match(first) and _PREAMBLE_META.search(first):
            idx += 1
            continue
        break
    if idx:
        return "\n".join(lines[idx:]).lstrip("\n")
    return text


def _raw_ask_text(sess, messages):
    """Drive a session's raw_ask generator; return its full text output."""
    gen = sess.raw_ask(messages)
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration:
        pass
    except Exception as e:
        return f"!!!Error: {type(e).__name__}: {e}"
    return "".join(chunks).strip()


def ocr(image_path, prompt=_DEFAULT_OCR_PROMPT,
        timeout=120, model=None, current=None):
    """OCR a local image, rotating through configured vision models.

    Order: explicit ``model`` (mykey config name) -> ``current`` (the caller's
    active model) -> ``VISION_FALLBACKS``. First successful extraction wins;
    raises with per-model errors when every model fails.
    """
    from llmcore import resolve_session
    mime, data = _read_image(image_path)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt or _DEFAULT_OCR_PROMPT},
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
    ]}]
    ordered = []
    for name in ([model] if model else []) + ([current] if current else []) + VISION_FALLBACKS:
        if name and name not in ordered:
            ordered.append(name)
    errors = []
    for name in ordered:
        try:
            sess = resolve_session(name)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if sess is None:
            errors.append(f"{name}: not a resolvable session")
            continue
        try:
            sess.read_timeout = max(int(getattr(sess, "read_timeout", 0) or 0), int(timeout))
            sess.max_retries = min(int(getattr(sess, "max_retries", 2) or 0), 2)
        except Exception:
            pass
        text = _raw_ask_text(sess, messages)
        if text and not text.startswith(("!!!Error:", "[!!!")):
            return _strip_preamble(text)
        errors.append(f"{name}: {(text or 'empty response')[:200]}")
    raise RuntimeError(f"all {len(ordered)} OCR models failed: " + " | ".join(errors))


def generate_image(prompt, size="1K", quality="auto", timeout=180, output_dir=None):
    import requests
    from uuid import uuid4
    key, cfg = _credential("image")
    if not key:
        raise RuntimeError("image2 credential is not configured")
    base = cfg["apibase"].rstrip("/")
    model = cfg["model"]
    payload = {"model": model, "prompt": prompt, "size": size,
               "quality": quality, "n": 1}
    # The upstream exposes no parameter schema; pass through values it accepts.
    r = requests.post(base + "/images/generations", headers={"Authorization": "Bearer " + key,
        "Content-Type": "application/json"}, json=payload, timeout=timeout)
    r.raise_for_status()
    item = (r.json().get("data") or [{}])[0]
    if item.get("url"):
        image_url = item["url"]
        download = requests.get(image_url, timeout=timeout)
        download.raise_for_status()
        content_type = download.headers.get("content-type", "image/png").split(";", 1)[0]
        suffix = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(content_type, ".png")
        out = Path(output_dir).expanduser().resolve() / "image" if output_dir else Path.cwd() / "image"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"generated_{uuid4().hex[:12]}{suffix}"
        target.write_bytes(download.content)
        return str(target)
    if item.get("b64_json"):
        out = Path(output_dir).expanduser().resolve() / "image" if output_dir else Path.cwd() / "image"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"generated_{uuid4().hex[:12]}.png"
        target.write_bytes(base64.b64decode(item["b64_json"]))
        return str(target)
    raise RuntimeError("image API returned neither url nor b64_json")
