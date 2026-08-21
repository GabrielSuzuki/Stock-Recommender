"""TokenWiseAgent -- a drop-in wrapper that makes each request cost less.

    from tokenwise import TokenWiseAgent
    agent = TokenWiseAgent()
    reply = agent.ask("Classify this ticket as billing/technical/other: ...")
    print(agent.summary())

Or as a near drop-in for the SDK client:

    resp = agent.messages.create(
        model="claude-opus-5",            # treated as the *baseline*; may be routed down
        max_tokens=1024,
        messages=[...],
    )

Every call is put through four passes and then billed against a
counterfactual, so the savings number is auditable rather than aspirational.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from . import tokens
from .caching import CacheOptimizer
from .compaction import Compactor
from .config import Config
from .ledger import Ledger, Record, summarize
from .pricing import cost
from .router import CLASSIFIER_PROMPT, Router
from .semantic_cache import SemanticCache


class _MessagesShim:
    def __init__(self, agent: "TokenWiseAgent") -> None:
        self._agent = agent

    def create(self, **kwargs: Any) -> Any:
        return self._agent.create(**kwargs)


class TokenWiseAgent:
    def __init__(
        self,
        client: Any = None,
        config: Optional[Config] = None,
        **overrides: Any,
    ) -> None:
        self.config = config or Config()
        for k, v in overrides.items():
            if not hasattr(self.config, k):
                raise TypeError(f"unknown config option: {k}")
            setattr(self.config, k, v)
        c = self.config

        self.client = client if client is not None else _default_client()
        self.router = Router(
            tiers=c.tiers,
            mode=c.routing,
            baseline=c.baseline_model,
            min_tier=c.min_tier,
        )
        self.cache_optimizer = CacheOptimizer(
            ttl=c.cache_ttl, message_breakpoints=c.message_breakpoints, enabled=c.prompt_cache
        )
        self.compactor = Compactor(
            max_context_tokens=c.max_context_tokens,
            tool_result_max_tokens=c.tool_result_max_tokens,
            keep_head=c.keep_head,
            keep_tail=c.keep_tail,
            dedupe=c.dedupe,
            drop_thinking=c.drop_thinking,
            summarizer=self._summarize if c.summarize_evicted else None,
            enabled=c.compaction,
        )
        self.semantic = SemanticCache(
            path=c.semantic_cache_path,
            threshold=c.semantic_threshold,
            ttl_seconds=c.semantic_ttl_seconds,
            embedder=c.embedder,
            enabled=c.semantic_cache,
            namespace=c.semantic_namespace,
        )
        self.ledger = Ledger(c.ledger_path, enabled=c.ledger)
        self.messages = _MessagesShim(self)
        self.last_record: Optional[Record] = None

    # -- public API --------------------------------------------------------
    def ask(self, prompt: str, system: Any = None, **kwargs: Any) -> str:
        resp = self.create(messages=[{"role": "user", "content": prompt}], system=system, **kwargs)
        return text_of(resp)

    def create(self, **kwargs: Any) -> Any:
        cfg = self.config
        started = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        tag = kwargs.pop("tag", "")

        messages: list[Any] = list(kwargs.pop("messages", []))
        system = kwargs.pop("system", None)
        tools = kwargs.pop("tools", None)
        baseline_model = kwargs.pop("model", None) or cfg.baseline_model
        kwargs.setdefault("max_tokens", cfg.max_tokens)
        stream = bool(kwargs.get("stream"))
        temperature = kwargs.get("temperature")
        notes: list[str] = []

        pre_in = (
            tokens.estimate_messages(messages)
            + tokens.estimate_system(system)
            + tokens.estimate_tools(tools)
        )

        # ---- pass 0: semantic cache -------------------------------------
        ok, why = self.semantic.cacheable(tools=tools, temperature=temperature, stream=stream)
        if ok:
            hit = self.semantic.lookup(messages, system)
            if hit is not None:
                out_est = hit.saved_output_tokens or tokens.estimate_text(hit.text)
                saved = cost(baseline_model, input_tokens=pre_in, output_tokens=out_est)
                rec = Record(
                    ts=time.time(),
                    request_id=request_id,
                    model=hit.model,
                    baseline_model=baseline_model,
                    actual_cost=0.0,
                    baseline_cost=saved,
                    saved={"semantic_cache": saved},
                    served_from_cache=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    notes=[f"{'exact' if hit.exact else 'similar'} hit sim={hit.similarity:.3f}"],
                    tag=tag,
                )
                self._record(rec)
                return CachedResponse(hit.text, hit.model, hit.similarity, hit.exact)
        elif why:
            notes.append(f"semantic-cache-skipped:{why}")

        # ---- pass 1: compaction -----------------------------------------
        messages, comp_report = self.compactor.compact(messages)
        if comp_report.actions:
            notes.extend(comp_report.actions)

        # ---- pass 2: routing --------------------------------------------
        decision = self.router.route(
            messages,
            system,
            classify=self._classify if cfg.routing == "classifier" else None,
            model=baseline_model,
            tools=tools,
            **{k: v for k, v in kwargs.items() if k in ("max_tokens", "thinking")},
        )
        model = decision.model

        # ---- pass 3: prompt cache breakpoints ---------------------------
        c_messages, c_system, c_tools, cache_report = self.cache_optimizer.apply(
            model, messages, system, tools
        )
        if cache_report.breakpoints:
            notes.append("cache breakpoints: " + ", ".join(cache_report.breakpoints))

        # ---- send --------------------------------------------------------
        resp = self._send(model, c_messages, c_system, c_tools, kwargs)
        usage = _usage(resp)
        escalated = False
        extra_cost = 0.0

        if cfg.escalate_on is not None and decision.tier != "strong":
            try:
                needs_more = bool(cfg.escalate_on(resp))
            except Exception:
                needs_more = False
            if needs_more:
                stronger = self.router.escalate(decision)
                if stronger:
                    extra_cost = _cost_of(model, usage)
                    notes.append(f"escalated {model} -> {stronger}")
                    escalated = True
                    e_messages, e_system, e_tools, _ = self.cache_optimizer.apply(
                        stronger, messages, system, tools
                    )
                    resp = self._send(stronger, e_messages, e_system, e_tools, kwargs)
                    usage = _usage(resp)
                    model = stronger

        # ---- accounting ---------------------------------------------------
        post_in = usage["input"] + usage["cache_write"] + usage["cache_read"]
        out = usage["output"]
        B, M = baseline_model, model

        baseline_cost = cost(B, input_tokens=max(pre_in, post_in), output_tokens=out)
        actual_cost = _cost_of(M, usage) + extra_cost + decision.classifier_cost

        saved_compaction = cost(B, input_tokens=max(pre_in, post_in)) - cost(B, input_tokens=post_in)
        saved_routing = cost(B, input_tokens=post_in, output_tokens=out) - cost(
            M, input_tokens=post_in, output_tokens=out
        )
        saved_cache = cost(M, input_tokens=post_in) - cost(
            M,
            input_tokens=usage["input"],
            cache_write_tokens=usage["cache_write"],
            cache_read_tokens=usage["cache_read"],
        )
        # Escalation retries and classifier calls are real money: charge them
        # back to routing so the headline number stays honest.
        saved_routing -= extra_cost + decision.classifier_cost

        rec = Record(
            ts=time.time(),
            request_id=request_id,
            model=M,
            baseline_model=B,
            input_tokens=usage["input"],
            output_tokens=out,
            cache_write_tokens=usage["cache_write"],
            cache_read_tokens=usage["cache_read"],
            actual_cost=actual_cost,
            baseline_cost=baseline_cost,
            saved={
                "compaction": saved_compaction,
                "routing": saved_routing,
                "prompt_cache": saved_cache,
            },
            escalated=escalated,
            latency_ms=(time.perf_counter() - started) * 1000,
            route_reasons=decision.reasons,
            notes=notes,
            tag=tag,
        )
        self._record(rec)

        if ok and not stream:
            body = text_of(resp)
            if body:
                self.semantic.store(messages, body, M, post_in, out, system)

        return resp

    # -- reporting ---------------------------------------------------------
    def summary(self, since: Optional[float] = None) -> dict:
        s = self.ledger.summary(since)
        s["semantic_cache"] = self.semantic.stats()
        return s

    def print_summary(self) -> None:
        from .report import render_text

        print(render_text(self.summary()))

    def close(self) -> None:
        self.semantic.close()

    # -- internals ---------------------------------------------------------
    def _send(self, model: str, messages: list, system: Any, tools: Any, kwargs: dict) -> Any:
        payload = dict(kwargs)
        payload["model"] = model
        payload["messages"] = messages
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        return self.client.messages.create(**payload)

    def _classify(self, text: str) -> int:
        resp = self.client.messages.create(
            model=self.config.tiers["cheap"],
            max_tokens=4,
            system=CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": text[:4000]}],
        )
        body = text_of(resp).strip()
        for ch in body:
            if ch in "123":
                return int(ch)
        return 2

    def _summarize(self, dropped: list[Any]) -> str:
        from .compaction import _content_of  # local import to avoid cycles

        blob = []
        for m in dropped:
            c = _content_of(m)
            if isinstance(c, str):
                blob.append(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        blob.append(b.get("text", ""))
        joined = "\n".join(blob)[:20_000]
        if not joined.strip():
            return ""
        resp = self.client.messages.create(
            model=self.config.tiers["cheap"],
            max_tokens=400,
            system=(
                "Compress this conversation excerpt into notes the assistant needs to keep "
                "working: decisions made, facts established, files/identifiers referenced, "
                "open questions. Bullet points, no preamble."
            ),
            messages=[{"role": "user", "content": joined}],
        )
        return text_of(resp)

    def _record(self, rec: Record) -> None:
        self.last_record = rec
        self.ledger.write(rec)
        if self.config.verbose:
            print(
                f"[tokenwise] {rec.model:<20} ${rec.actual_cost:.5f} "
                f"(baseline ${rec.baseline_cost:.5f}, saved ${rec.total_saved:.5f}) "
                + ("| ".join(rec.notes) if rec.notes else "")
            )


class CachedResponse:
    """Minimal stand-in for a Message when we answer from the semantic cache."""

    def __init__(self, text: str, model: str, similarity: float, exact: bool) -> None:
        self.content = [{"type": "text", "text": text}]
        self.model = model
        self.stop_reason = "end_turn"
        self.role = "assistant"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.tokenwise_cache = {"similarity": similarity, "exact": exact}


def text_of(resp: Any) -> str:
    content = getattr(resp, "content", None)
    if content is None and isinstance(resp, dict):
        content = resp.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
        elif getattr(b, "type", None) == "text":
            parts.append(getattr(b, "text", ""))
    return "".join(parts)


def _usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None) or (resp.get("usage") if isinstance(resp, dict) else None) or {}
    get = (lambda k: u.get(k, 0)) if isinstance(u, dict) else (lambda k: getattr(u, k, 0) or 0)
    return {
        "input": get("input_tokens") or 0,
        "output": get("output_tokens") or 0,
        "cache_write": get("cache_creation_input_tokens") or 0,
        "cache_read": get("cache_read_input_tokens") or 0,
    }


def _cost_of(model: str, usage: dict[str, int]) -> float:
    return cost(
        model,
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        cache_write_tokens=usage["cache_write"],
        cache_read_tokens=usage["cache_read"],
    )


def _default_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The anthropic SDK is not installed. Run `pip install anthropic`, or pass your "
            "own client: TokenWiseAgent(client=my_client)."
        ) from exc
    return anthropic.Anthropic()
