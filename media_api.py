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


# Vision-capable mykey config names, used ONLY as last-resort remote fallback
# when no local OCR tool is installed (user preference: local OCR first, no
# blind remote rotation).
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

_VISION_NAME_HINTS = ("vl", "vision", "omni")


def _model_supports_vision(name):
    """判断 mykey 配置是否支持图片输入。True/False/None(未知)。

    优先级：cfg 显式 'vision' 字段 > 本地端点 /v1/models capabilities 实测
    （llama.cpp 等会自报 multimodal）> 模型名启发式（vl/vision/omni）。
    远程端点无显式字段时返回 None（不盲猜，交给本地 OCR）。
    """
    try:
        cfg = getattr(importlib.import_module("mykey"), name)
    except (ImportError, AttributeError):
        return None
    if not isinstance(cfg, dict):
        return None
    if "vision" in cfg:
        return bool(cfg["vision"])
    base = (cfg.get("apibase") or "").lower()
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        try:
            import requests
            r = requests.get(base.rstrip("/") + "/models", timeout=5)
            caps = [str(c).lower() for c in ((r.json().get("models") or [{}])[0].get("capabilities") or [])]
            return "multimodal" in caps
        except Exception:
            return None
    ml = (cfg.get("model") or "").lower()
    if any(t in ml for t in _VISION_NAME_HINTS):
        return True
    return None


# 本地 OCR 引擎单例缓存（初始化要加载模型，秒级~十秒级，不能每次调用重建）
_LOCAL_OCR_ENGINES = {}


def _local_ocr_rapidocr(path):
    eng = _LOCAL_OCR_ENGINES.get("rapid")
    if eng is None:
        from rapidocr_onnxruntime import RapidOCR
        eng = _LOCAL_OCR_ENGINES["rapid"] = RapidOCR()
    res, _el = eng(str(path))
    if not res:
        return ""
    return "\n".join(r[1] for r in res)


def _local_ocr_easyocr(path):
    eng = _LOCAL_OCR_ENGINES.get("easy")
    if eng is None:
        import easyocr
        eng = _LOCAL_OCR_ENGINES["easy"] = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    out = eng.imread(str(path))
    return "\n".join(line for line, _score in (out or []) if line)


def _local_ocr_tesseract(path):
    import shutil
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract not installed")
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(str(path)))

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
    """Drive a session's raw_ask generator; return its final clean text.

    取 StopIteration 里的 MockResponse.content（干净正文），而不是显示流拼接——
    显示流含思考信封标签等展示用包装，直接收进 OCR 结果会混入标签文本。
    """
    gen = sess.raw_ask(messages)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        resp = e.value
        if resp is not None and getattr(resp, "content", None):
            return (resp.content or "").strip()
        return f"!!!Error: empty response (stop_reason={getattr(resp, 'stop_reason', '?')})"
    except Exception as e:
        return f"!!!Error: {type(e).__name__}: {e}"


def ocr(image_path, prompt=_DEFAULT_OCR_PROMPT,
        timeout=120, model=None, current=None):
    """OCR a local image.

    优先级（用户偏好：能看图的模型直调，否则本地 OCR，不盲轮远程模型）：
      1. 显式 ``model``（mykey 配置名）：单次远程调用，失败即报错。
      2. ``current``（调用方当前模型）：支持图片输入则直调，失败落到本地。
      3. 本地 OCR 引擎链：rapidocr -> easyocr -> tesseract（需安装）。
      4. 本地引擎全不可用/失败：回退远程 ``VISION_FALLBACKS`` 轮换。
    """
    from llmcore import resolve_session
    mime, data = _read_image(image_path)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt or _DEFAULT_OCR_PROMPT},
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
    ]}]

    errors = []

    def _remote_once(name):
        sess = resolve_session(name)
        if sess is None:
            raise RuntimeError(f"{name}: not a resolvable session")
        try:
            sess.read_timeout = max(int(getattr(sess, "read_timeout", 0) or 0), int(timeout))
            sess.max_retries = min(int(getattr(sess, "max_retries", 2) or 0), 2)
        except Exception:
            pass
        text = _raw_ask_text(sess, messages)
        if text and not text.startswith(("!!!Error:", "[!!!")):
            return _strip_preamble(text)
        raise RuntimeError(f"{name}: {(text or 'empty response')[:200]}")

    # 1) 显式 model：用户点名，单次
    if model:
        return _remote_once(model)

    # 2) 当前模型能看图就直调
    if current:
        if _model_supports_vision(current) is True:
            try:
                return _remote_once(current)
            except Exception as e:
                errors.append(str(e))
        else:
            errors.append(f"{current}: no image input support (skipped)")

    # 3) 本地 OCR 引擎链
    local_errors = []
    for fn in (_local_ocr_rapidocr, _local_ocr_easyocr, _local_ocr_tesseract):
        try:
            text = fn(image_path)
            if text and text.strip():
                return text.strip()
            local_errors.append(f"{fn.__name__}: empty result")
        except Exception as e:
            local_errors.append(f"{fn.__name__}: {type(e).__name__}: {str(e)[:120]}")

    # 4) 本地全失败/不可用：远程轮换兜底
    for name in VISION_FALLBACKS:
        if name == current:
            continue
        try:
            return _remote_once(name)
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(
        f"local OCR failed ({' | '.join(local_errors)}) and all "
        f"{len(VISION_FALLBACKS)} remote vision models failed: " + " | ".join(errors))


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
