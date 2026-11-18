"""End-to-end check of the runtime model discovery.

Drives the real MCP server over stdio and asks Gemini itself which models the
account is offered. Nothing is mocked: the server talks to gemini.google.com.

The pool used is the one the server normally reads, so the test needs a live
session. A dead session is reported as a failure rather than silently skipped.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_common import call_tool, initialize, list_tools, log, report, start_server, stop  # noqa: E402

HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "MyGeminiWebMcp"
ACCOUNT = os.environ.get("GEMINIWEB_TEST_ACCOUNT", "kimperiod0126")

#: How long to wait between two chat turns. Gemini throttles a session that is
#: hammered with requests, so the test is deliberately slow.
GAP = int(os.environ.get("GEMINIWEB_TEST_GAP", "20"))


def main():
    failures = []
    log("=" * 66)
    log(f"  E2E: runtime model discovery ({ACCOUNT})")
    log("=" * 66)

    proc, token, endpoint = start_server(HOME)
    try:
        initialize(proc)
        tools = list_tools(proc)
        log(f"[*] tools: {tools}")
        if "gemini_models" not in tools:
            failures.append("the gemini_models tool is missing")

        # 1) Discovery: what does this account get offered?
        log("\n[*] gemini_models")
        res = call_tool(proc, "gemini_models", {"account": ACCOUNT})
        models = res.get("models") or []
        log(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
        if not models:
            failures.append(
                "gemini_models returned no models — the session is probably dead")
        for m in models:
            log(f"    {m['name']:24s} id={m['modelId']} n={m['modelNumber']} "
                f"cap={m['capacity']} display={m['displayName']!r} desc={m['description']!r}")

        # 2) Every spelling of a model must resolve to the same model.
        log("\n[*] gemini_chat with each discovered model")
        for m in models:
            for name in (m["name"], m["description"], m["displayName"]):
                t0 = time.time()
                r = call_tool(proc, "gemini_chat", {
                    "prompt": "Say the number 7 only",
                    "account": ACCOUNT,
                    "model": name,
                })
                reply = r.get("reply") or ""
                err = r.get("error") or ""
                served = r.get("servedModel")
                notice = r.get("quotaNotice")
                ok = "7" in reply
                log(f"    {'PASS' if ok else 'FAIL'} {name:26s} "
                    f"({m['name']}, n={m['modelNumber']}) in {time.time() - t0:5.1f}s "
                    f"-> {reply[:40]!r} served={served!r} notice={notice!r} {err[:70]!r}")
                if not ok:
                    failures.append(f"{name} ({m['name']}) -> {reply[:100]!r} {err[:100]!r}")
                if not served:
                    failures.append(f"{name} ({m['name']}) did not report servedModel")
                if notice:
                    # Google quietly downgraded the answer: the quota of the requested
                    # model was exhausted. The reply is not from the model asked for.
                    failures.append(
                        f"{name} ({m['name']}) was downgraded by Google: {notice!r}")
                time.sleep(GAP)

        # 3) An unknown model name must fail cleanly and list the options.
        log("\n[*] gemini_chat with an unknown model")
        res = call_tool(proc, "gemini_chat", {
            "prompt": "hi",
            "account": ACCOUNT,
            "model": "definitely-not-a-model",
        })
        log(json.dumps(res, ensure_ascii=False)[:400])
        if "Unknown model" not in json.dumps(res):
            failures.append("an unknown model name did not produce a clean error")
    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())