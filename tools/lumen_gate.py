"""CI release gate: evaluate a candidate's outputs on a Lumen dataset and fail the build if the gate fails.

    # 1) get the dataset, run your candidate on every item, write outputs.jsonl  ({"item_id": ..., "output": ...} per line)
    python tools/lumen_gate.py export --key $LUMEN_KEY --dataset rag-golden > items.jsonl
    # 2) gate it (exit code 0 = pass, 1 = fail)
    python tools/lumen_gate.py gate --key $LUMEN_KEY --dataset rag-golden --name "PR-123 rag v5" \
        --prompt-name rag_answer --prompt-version 5 --outputs outputs.jsonl
Standard library only. The key is passed on the command line or via your CI secret store - Lumen never reads it from env itself.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request


def call(host, key, path, body=None):
    req = urllib.request.Request(host.rstrip("/") + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Lumen-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Lumen error {e.code}: {e.read().decode()[:300]}")


def main():
    ap = argparse.ArgumentParser(description="Lumen CI release gate")
    ap.add_argument("cmd", choices=["export", "gate"])
    ap.add_argument("--host", default="http://127.0.0.1:8100")
    ap.add_argument("--key", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--name", default="ci candidate")
    ap.add_argument("--prompt-name")
    ap.add_argument("--prompt-version")
    ap.add_argument("--model")
    ap.add_argument("--outputs", help="JSONL with item_id and output")
    a = ap.parse_args()
    if a.cmd == "export":
        for it in call(a.host, a.key, f"/v1/datasets/{a.dataset}")["items"]:
            print(json.dumps(it))
        return
    outs = {}
    with open(a.outputs, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                o = json.loads(line)
                outs[o["item_id"]] = o
    r = call(a.host, a.key, "/v1/experiments", {"dataset": a.dataset, "name": a.name, "prompt_name": a.prompt_name,
                                                "prompt_version": a.prompt_version, "model": a.model, "outputs": outs})
    g = r["gate"]
    print(f"experiment {r['experiment']}  pass rate {r['summary']['pass_rate']:.1%}  baseline {g['baseline']}")
    for c in g["checks"]:
        print(f"  [{c['result'].upper():4}] {c['check']}: {c['detail']}")
    print("GATE PASSED" if g["passed"] else "GATE FAILED")
    sys.exit(0 if g["passed"] else 1)


if __name__ == "__main__":
    main()
