"""Run after filling .env: python check_key.py"""

import os
import sys
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

import extract

b = extract.backend()

print("Extractor in use:", b)

try:
    if b == "local":
        print("No API key needed. Local mode is active.")

    elif b == "groq":
        client = Groq(
            api_key=os.environ["GROQ_API_KEY"]
        )

        response = client.chat.completions.create(
            model=os.getenv(
                "GROQ_MODEL",
                "qwen/qwen3.8-27b"
            ),
            messages=[
                {"role": "user", "content": "Reply with OK"}
            ],
            max_completion_tokens=20
        )

        print(
            "Groq key and model work:",
            response.choices[0].message.content
        )

    elif b == "gemini":
        import httpx

        model = os.getenv("GEMINI_MODEL")

        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={
                "x-goog-api-key": os.environ["GEMINI_API_KEY"]
            },
            timeout=30,
            json={
                "contents": [
                    {"parts": [{"text": "Reply with OK"}]}
                ]
            }
        )

        print(r.status_code, r.text[:500])

    elif b == "anthropic":
        import anthropic

        client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )

        response = client.messages.create(
            model=os.getenv("MODEL", "claude-sonnet-5-5"),
            max_tokens=20,
            messages=[
                {"role": "user", "content": "Reply with OK"}
            ]
        )

        print("Anthropic works:", response.content[0].text)

except Exception as e:
    sys.exit(f"Failed: {e}")