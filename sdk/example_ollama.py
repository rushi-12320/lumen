"""Two-line setup with a real model and no cloud API key: Ollama's OpenAI-compatible endpoint on your machine.

    ollama pull llama3.1
    pip install openai  https://<your-lumen>/sdk/lumen_llm_sdk-0.2.0-py3-none-any.whl
    python example_ollama.py --key lmn_... --host https://<your-lumen>

Each question becomes one trace in Lumen: a retrieval span (the help-center article) and the LLM call, recorded by
lumen_sdk.init() without any other change to the code. The groundedness check compares the answer with the article.
"""
import argparse

from openai import OpenAI

import lumen_sdk

ARTICLES = {
    "password": "To reset your password open Settings, choose Security and click Reset password.",
    "invoice": "Invoices can be exported from Billing, then History, using the Export CSV button.",
    "rate limit": "The API allows 600 requests per minute per workspace; bursts above that limit receive HTTP status 429.",
}
QUESTIONS = ["How do I reset my password?", "How can I export my invoices?", "What is the API rate limit?"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="a Lumen API key (Get started, or Settings & Keys)")
    ap.add_argument("--host", default="http://127.0.0.1:8100")
    ap.add_argument("--model", default="llama3.1")
    ap.add_argument("--ollama", default="http://localhost:11434/v1")
    a = ap.parse_args()

    lumen_sdk.init(api_key=a.key, host=a.host)                     # line 2 of the two-line setup
    llm = OpenAI(base_url=a.ollama, api_key="ollama")              # Ollama ignores the key

    for q in QUESTIONS:
        with lumen_sdk.trace("support_answer", input=q, user_id="example-user") as t:
            with t.span("retrieval", name="kb_search", input=q) as s:
                doc = next((v for k, v in ARTICLES.items() if k in q.lower()), "")
                s.output = doc
            r = llm.chat.completions.create(model=a.model, temperature=0.2, messages=[
                {"role": "system", "content": f"Answer in one sentence, only from this article: {doc}"},
                {"role": "user", "content": q}])
            t.output = r.choices[0].message.content
        print(f"Q: {q}\nA: {t.output}\n")
    lumen_sdk.flush()
    print(f"Sent {len(QUESTIONS)} traces to {a.host}. Open Traces in the console.")


if __name__ == "__main__":
    main()
