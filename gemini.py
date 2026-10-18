import base64
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "")
if not KEY:
    raise SystemExit("GEMINI_API_KEY is missing in the .env file")

CATEGORIES = ["glass", "plastic", "paper"]
PROMPT = (
    "You sort waste for a school recycling bin. Look at the photo and answer "
    "with exactly one word: glass, plastic, paper, or none "
    "(none = it is something else, or no trash is visible)."
)


def pick_model():
    """Ask Google which models exist, and choose a plain Flash one."""
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
        headers={"x-goog-api-key": KEY},
    )
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    bad = ("image", "live", "tts", "audio", "embedding", "thinking", "robotics")
    names = [
        m["name"] for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
        and "flash" in m["name"]
        and not any(b in m["name"] for b in bad)
    ]
    if not names:
        raise SystemExit("No Flash model found for your key")
    return sorted(names)[-1]          # usually the newest one


def classify(path):
    mime = {"png": "image/png", "webp": "image/webp"}.get(
        path.lower().rsplit(".", 1)[-1], "image/jpeg"
    )
    with open(path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    model = MODEL or pick_model()
    model_path = model if model.startswith("models/") else "models/" + model
    body = {
        "contents": [{"parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": mime, "data": image_b64}},
        ]}],
        "generationConfig": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": KEY},
        method="POST",
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        return model_path, "error " + str(e.code), e.read().decode()[:200]

    try:
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return model_path, "unknown", json.dumps(resp)[:200]

    answer = text.strip().lower()
    category = next((c for c in CATEGORIES if c in answer), "none")
    return model_path, category, text.strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python test_photo.py photo.jpg")
    model, category, raw = classify(sys.argv[1])
    print("Model:    ", model)
    print("Category: ", category)
    print("Raw reply:", raw)