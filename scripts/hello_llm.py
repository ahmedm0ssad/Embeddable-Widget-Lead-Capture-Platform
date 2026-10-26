"""
Smoke-test the LLM provider used by the A17 /books/classify feature.

Usage:
    python scripts/hello_llm.py

Requires LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL in .env or environment
(see .env.example).
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

REQUIRED_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def main() -> None:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        print(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + " — set them in .env (see .env.example) and retry."
        )
        sys.exit(1)

    client = OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],  # used internally, never printed
    )
    res = client.chat.completions.create(
        model=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": "Reply with exactly the word: ready"}],
    )
    print(res.choices[0].message.content)


if __name__ == "__main__":
    main()
