"""Print the EXACT openai-compatible response from a model. Helps catch
cases where content lives in reasoning_content instead of content."""

import asyncio
import json
import sys
from openai import AsyncOpenAI


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


async def main():
    if len(sys.argv) < 2:
        print("usage: probe_raw.py <model>")
        return
    model = sys.argv[1]
    client = AsyncOpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    r = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You output strict JSON. No prose."},
            {"role": "user", "content": (
                'Output a JSON object: {"subject":"iPhone 16 Pro Max",'
                '"fields":{"weight_g":{"value":227,"confidence":0.95}}}. '
                "Nothing else.\n\n/no_think"
            )},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    msg = r.choices[0].message
    print("--- finish_reason:", r.choices[0].finish_reason)
    print("--- content (len {}) ---".format(len(msg.content or "")))
    print(repr(msg.content))
    for attr in ("reasoning_content", "reasoning"):
        v = getattr(msg, attr, None)
        if v:
            print(f"--- {attr} (len {len(v)}) ---")
            print(repr(v))
    print("--- usage ---")
    print(r.usage)


if __name__ == "__main__":
    asyncio.run(main())
