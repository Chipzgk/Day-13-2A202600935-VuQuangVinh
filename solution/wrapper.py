"""Thread-safe observability and mitigations around the opaque agent."""
from __future__ import annotations

import re
import time
import unicodedata

from telemetry.cost import cost_from_usage
from telemetry.logger import logger, new_correlation_id, set_correlation_id
from telemetry.redact import redact


_RETRYABLE = {"loop", "max_steps", "no_action", "wrapper_error"}


def _plain(text):
    normalized = unicodedata.normalize("NFKD", text or "")
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _request_facts(question):
    text = _plain(question)
    quantity_match = re.search(r"\b(?:mua|dat|lay|can|muon)\s+(\d+)\b", text)
    return {
        "quantity": int(quantity_match.group(1)) if quantity_match else 1,
        "coupon": bool(
            re.search(r"\b(coupon|ma|ap dung|khuyen mai|promo)\b", text)
        ),
        "shipping": bool(re.search(r"\b(giao|ship|chuyen den)\b", text)),
    }


def _observations(result):
    found = {}
    for entry in result.get("trace") or []:
        tool = entry.get("tool")
        observation = entry.get("observation")
        if tool and isinstance(observation, dict):
            found[tool] = observation
    return found


def _needs_retry(result, facts):
    if result.get("status") in _RETRYABLE:
        return True

    obs = _observations(result)
    stock = obs.get("check_stock")
    if not stock or stock.get("error") == "bad_arguments":
        return True
    if not stock.get("found", True) or not stock.get("in_stock", False):
        return False
    if int(stock.get("quantity", 0)) < facts["quantity"]:
        return False
    if facts["coupon"] and (
        "get_discount" not in obs or obs["get_discount"].get("error")
    ):
        return True
    if facts["shipping"] and (
        "calc_shipping" not in obs
        or obs["calc_shipping"].get("error") == "bad_arguments"
    ):
        return True
    return False


def _ground_answer(result, facts):
    """Use complete tool evidence to prevent fabrication and arithmetic errors."""
    obs = _observations(result)
    stock = obs.get("check_stock")
    if not stock:
        return result

    if (
        not stock.get("found", True)
        or not stock.get("in_stock", False)
        or int(stock.get("quantity", 0)) < facts["quantity"]
    ):
        grounded = dict(result)
        grounded["answer"] = "San pham khong co san hoac khong du hang."
        return grounded
    if stock.get("error"):
        return result

    discount = obs.get("get_discount")
    shipping = obs.get("calc_shipping")
    if facts["coupon"] and not discount:
        return result
    if facts["shipping"] and not shipping:
        return result
    if facts["shipping"] and shipping.get("cost_vnd") is None:
        grounded = dict(result)
        grounded["answer"] = "Khong the giao den dia diem nay."
        return grounded

    try:
        unit_price = int(stock["unit_price_vnd"])
        percent = int(discount.get("percent", 0)) if discount else 0
        shipping_cost = int(shipping.get("cost_vnd", 0)) if shipping else 0
        subtotal = unit_price * facts["quantity"]
        total = subtotal * (100 - percent) // 100 + shipping_cost
    except (KeyError, TypeError, ValueError):
        return result

    grounded = dict(result)
    grounded["answer"] = "Tong cong: {} VND".format(total)
    return grounded


def _log_result(result, context, wall_ms, attempt, cache_hit=False, error=None):
    """Telemetry must never break an otherwise valid request."""
    try:
        meta = result.get("meta") or {}
        usage = meta.get("usage") or {}
        answer = result.get("answer") or ""
        logger.log_event("AGENT_CALL", {
            "qid": context.get("qid"),
            "session_id": context.get("session_id"),
            "turn_index": context.get("turn_index"),
            "status": result.get("status"),
            "attempt": attempt,
            "cache_hit": cache_hit,
            "wall_ms": wall_ms,
            "latency_ms": meta.get("latency_ms"),
            "tokens": usage,
            "cost_usd": cost_from_usage(meta.get("model", ""), usage),
            "steps": result.get("steps"),
            "tools_used": meta.get("tools_used", []),
            "pii_redactions": redact(answer)[1],
            "trace": result.get("trace", []),
            "error": error,
        })
    except Exception:
        pass


def _redact_answer(result):
    answer = result.get("answer")
    if isinstance(answer, str):
        cleaned, _ = redact(answer)
        if cleaned != answer:
            result = dict(result)
            result["answer"] = cleaned
    return result


def mitigate(call_next, question, config, context):
    set_correlation_id(str(context.get("qid") or new_correlation_id()))
    cache = context.get("cache")
    lock = context.get("cache_lock")
    cache_key = question.strip().casefold()

    if cache is not None and lock is not None:
        with lock:
            cached = cache.get(cache_key)
        if cached is not None:
            result = dict(cached)
            _log_result(result, context, 0, 0, cache_hit=True)
            return result

    retry_conf = config.get("retry") or {}
    attempts = max(1, int(retry_conf.get("max_attempts", 1)))
    backoff_ms = max(0, int(retry_conf.get("backoff_ms", 0)))
    facts = _request_facts(question)
    result = {"answer": None, "status": "wrapper_error"}
    conf = dict(config)

    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            result = call_next(question, conf)
        except Exception as exc:
            result = {"answer": None, "status": "wrapper_error"}
            error = "{}: {}".format(type(exc).__name__, str(exc)[:300])
        else:
            error = None
        wall_ms = int((time.time() - started) * 1000)
        result = _redact_answer(result)
        _log_result(result, context, wall_ms, attempt, error=error)
        if _needs_retry(result, facts) and attempt < attempts:
            conf = dict(conf)
            conf["max_completion_tokens"] = max(
                2400, int(conf.get("max_completion_tokens", 0))
            )
            if backoff_ms:
                time.sleep(backoff_ms / 1000)
            continue
        if not _needs_retry(result, facts):
            break
        if attempt < attempts and backoff_ms:
            time.sleep(backoff_ms / 1000)

    result = _ground_answer(result, facts)
    result = _redact_answer(result)
    if result.get("status") == "ok" and cache is not None and lock is not None:
        with lock:
            cache[cache_key] = dict(result)
    return result
