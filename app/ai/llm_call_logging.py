"""Consistent, secret-safe terminal logging for outbound LLM calls."""
from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Dict, Iterator, Optional
from uuid import uuid4


@contextmanager
def log_llm_call(
    *,
    provider: str,
    model: str,
    api: str,
    operation: Optional[str] = None,
) -> Iterator[Dict[str, object]]:
    """Print provider/model/timing details without logging credentials or prompts.

    Yields a mutable dict; callers may set keys (e.g. tokens, finish_reason,
    score) and they are printed on the DONE line. Callers should also do their
    response parsing INSIDE the context so "DONE" means "response is usable",
    not merely "HTTP returned".
    """
    call_id = uuid4().hex[:8]
    started_at = perf_counter()
    label = operation or "LLM request"
    separator = "=" * 78
    info: Dict[str, object] = {}

    print(f"\n{separator}")
    print(f"🤖 LLM CALL START   id={call_id} operation={label}")
    print(f"   provider={provider}")
    print(f"   model={model}")
    print(f"   api={api}")
    print(separator)

    try:
        yield info
    except Exception as exc:
        elapsed = perf_counter() - started_at
        print(f"❌ LLM CALL FAILED  id={call_id} elapsed={elapsed:.3f}s")
        print(f"   provider={provider} model={model}")
        print(f"   error={type(exc).__name__}: {exc}")
        print(f"{separator}\n")
        raise
    else:
        elapsed = perf_counter() - started_at
        print(f"✅ LLM CALL DONE    id={call_id} elapsed={elapsed:.3f}s")
        print(f"   provider={provider} model={model}")
        extras = " ".join(f"{k}={v}" for k, v in info.items() if v is not None)
        if extras:
            print(f"   {extras}")
        print(f"{separator}\n")
