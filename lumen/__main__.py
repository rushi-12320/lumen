import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser(description="Lumen - LLM observability, evaluation & ops agent")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    a = ap.parse_args()
    print(f"Lumen running on http://{a.host}:{a.port}")
    uvicorn.run("lumen.app:app", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
