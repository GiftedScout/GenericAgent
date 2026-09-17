"""Small OpenAI-compatible vision/image adapter.

Credentials are loaded from explicit dictionaries in ``mykey.py`` at call time.
Values are never logged or returned.
"""
import base64
import importlib
import os
from pathlib import Path


# `ocr` is a purely LOCAL OCR tool: it runs a local engine and never calls any
# cloud vision model.  Image understanding for the *agent itself* happens by
# attaching the image to the user message (see agentmain.run), not here.

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

_LOCAL_OCR_CHAIN = ("_local_ocr_rapidocr", "_local_ocr_easyocr", "_local_ocr_tesseract")

_INSTALL_HINT = ("no local OCR engine available; install one with "
                 "`python3 -m pip install --break-system-packages rapidocr_onnxruntime`")


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


def ocr(image_path, prompt=None,
        timeout=120, model=None, current=None):
    """OCR a local image with a LOCAL engine only.

    纯本地原子工具：rapidocr -> easyocr -> tesseract，绝不调用任何云端/多模态
    模型（用户要求）。`prompt` / `model` / `current` / `timeout` 仅为兼容旧调用
    签名而保留，本地引擎不使用它们。让多模态模型"看图"应由调用方把图片作为
    消息图片块附带，而不是绕道这里。

    本地引擎全部缺失/失败时抛 RuntimeError 并给出安装提示——不静默回退远程。
    """
    local_errors = []
    for name in _LOCAL_OCR_CHAIN:
        fn = globals()[name]
        try:
            text = fn(image_path)
            if text and text.strip():
                return text.strip()
            local_errors.append(f"{name}: empty result")
        except Exception as e:
            local_errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError(f"local OCR failed [{'; '.join(local_errors)}]; {_INSTALL_HINT}")


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
