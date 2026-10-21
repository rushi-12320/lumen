"""Example: a tiny RAG assistant instrumented with the Lumen SDK (no real LLM - a fake one so it runs anywhere).

    python sdk/example_app.py --key lmn_xxx            # create the key in Lumen > Settings > API keys
"""
import argparse
import random
import time

from lumen_sdk import Lumen

KB = {
    "password": "To reset your password open Settings, choose Security and click Reset password.",
    "invoice": "Invoices can be exported from Billing, then History, using the Export CSV button.",
    "sso": "SAML single sign-on is configured in Security, Single sign-on, by uploading the identity provider metadata file.",
}


def fake_llm(prompt):
    time.sleep(random.uniform(0.05, 0.2))
    for k, v in KB.items():
        if k in prompt.lower():
            return v
    return "I'm not sure - please contact support."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--host", default="http://127.0.0.1:8100")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    lumen = Lumen(api_key=a.key, host=a.host)
    try:
        p = lumen.get_prompt("rag_answer")
        print(f"using production prompt rag_answer v{p['version']}")
        version = p["version"]
    except Exception as e:
        print("prompt registry not reachable / no prompt yet:", e)
        version = None
    questions = ["How do I reset my password?", "How can I export my invoice?", "How do I enable SSO?", "Do you have a fax number?"]
    for _ in range(a.n):
        q = random.choice(questions)
        with lumen.start_trace("rag_answer", input=q, prompt_name="rag_answer", prompt_version=version, user_id="demo-user",
                               segment="pro") as t:
            with t.span("retrieval", name="kb_search", input=q) as s:
                s.output = " ".join(KB.values())
            with t.span("llm", name="generate", model="fast-model", input=q) as s:
                s.output = fake_llm(q)
                s.tokens_in, s.tokens_out = 120, len(s.output) // 4
            t.output = s.output
    lumen.flush()
    print(f"sent {a.n} traces (dropped {lumen.dropped})")


if __name__ == "__main__":
    main()
