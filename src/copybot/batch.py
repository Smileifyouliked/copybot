"""Reconciling batch API responses by ID.

Polymarket has now been observed silently dropping items from a batch response
twice, both with HTTP 200 and no error:

  * gamma /markets omits unknown condition_ids
  * CLOB POST /books omits tokens that have no book (e.g. resolved markets)

So the rule for this codebase, without exception: **never zip a request list
against a response list by index.** Always key the response by its own
identifier, look up what you asked for, and treat every absence as an explicit
missing value.

Getting this wrong does not raise. It silently attributes one token's book to a
different token, which marks a position at the wrong price and makes equity
quietly wrong -- the worst kind of bug, because the number still looks
plausible.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Sequence, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class BatchMismatch(Exception):
    """A batch response could not be keyed back to what was requested."""


def reconcile_batch(
    requested: Sequence[str],
    response: Iterable[T],
    key_of: Callable[[T], str | None],
    *,
    what: str,
    strict: bool = False,
) -> tuple[dict[str, T], list[str], list[str]]:
    """Key a batch response by ID and report what is missing or unexpected.

    Returns (by_id, missing, unexpected).

    `missing`    - requested but absent from the response. Normal for resolved
                   tokens; the caller must handle it as "no data", never as
                   zero and never by falling back to a neighbouring item.
    `unexpected` - present in the response but never requested. Always a red
                   flag: it means the response does not correspond to the
                   request, so nothing in it can be trusted positionally.
    """
    wanted = list(dict.fromkeys(requested))
    wanted_set = set(wanted)

    by_id: dict[str, T] = {}
    unexpected: list[str] = []
    unkeyed = 0

    for item in response:
        key = key_of(item)
        if not key:
            unkeyed += 1
            continue
        if key in wanted_set:
            by_id[key] = item
        else:
            unexpected.append(key)

    missing = [k for k in wanted if k not in by_id]

    if unkeyed:
        log.warning("%s: %d response item(s) carried no identifier and were discarded",
                    what, unkeyed)
    if missing:
        log.warning("%s: requested %d, received %d, missing %d -> %s",
                    what, len(wanted), len(by_id), len(missing),
                    ", ".join(_short(m) for m in missing[:5]) +
                    (" …" if len(missing) > 5 else ""))
    if unexpected:
        # Never silently absorb these: an unrequested id means the response is
        # not the response to this request.
        log.warning("%s: response contained %d id(s) we never asked for -> %s",
                    what, len(unexpected),
                    ", ".join(_short(u) for u in unexpected[:5]))
        if strict:
            raise BatchMismatch(
                f"{what}: response contained unrequested ids: {unexpected[:5]}"
            )

    return by_id, missing, unexpected


def assert_complete_page(rows: Sequence, limit: int, *, what: str) -> bool:
    """Warn when a paged response came back exactly full.

    The set-reconciliation rule above covers calls that name what they want.
    A paged call names only a count, so its silent-omission failure mode is
    different: a full page means the server had at least as many rows as we
    asked for, and anything beyond the limit was dropped without a signal.

    Returns True when the page is full (i.e. possibly truncated).
    """
    if limit and len(rows) >= limit:
        log.warning(
            "%s: returned a full page of %d, so older rows may exist beyond the "
            "limit and were not seen", what, len(rows),
        )
        return True
    return False


def _short(value: str) -> str:
    return value if len(value) <= 20 else f"{value[:17]}…"
