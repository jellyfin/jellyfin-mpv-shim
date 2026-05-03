"""
Power-user language preference rules.

A rule list is evaluated in order; the first rule that matches sets the
audio/subtitle tracks. A rule "matches" only when all of its constraints can
be satisfied — partial matches fall through to the next rule, and if no rule
matches, the existing Jellyfin server defaults apply.

Constraint fields (all reject the rule on no-match):
  type     - "movie" or "series" (item-type filter)
  alang    - mpv-style comma list of audio language priorities
  slang    - same for subtitles
  amatch   - regex over audio track titles
  smatch   - regex over subtitle track titles
  subtype  - "signs" or "full" (uses the bulk_subtitle weight helpers)

Bias fields (narrow the candidate set without rejecting the rule):
  aprefer  - regex over audio track titles, applied after alang
  sprefer  - same for subtitles
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, fields
from typing import List, Optional, Tuple

log = logging.getLogger("language_config")


@dataclass
class LanguageRule:
    type: Optional[str] = None
    alang: Optional[str] = None
    slang: Optional[str] = None
    aprefer: Optional[str] = None
    sprefer: Optional[str] = None
    amatch: Optional[str] = None
    smatch: Optional[str] = None
    subtype: Optional[str] = None

    def _to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def parse_language_config(value) -> Optional[List[LanguageRule]]:
    if value is None or value == "null":
        return None
    if not isinstance(value, list):
        log.error("language_config must be a list of rule objects, got %r", type(value))
        return None

    known = {f.name for f in fields(LanguageRule)}
    rules = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            log.warning("language_config[%d] is not an object, skipping", i)
            continue
        unknown = set(item.keys()) - known
        if unknown:
            log.warning(
                "language_config[%d] has unknown fields %s (ignored)",
                i, sorted(unknown),
            )
        filtered = {k: v for k, v in item.items() if k in known}
        try:
            rules.append(LanguageRule(**filtered))
        except Exception:
            log.warning("language_config[%d] failed to parse, skipping", i, exc_info=True)
    return rules


def _track_title(stream: dict) -> str:
    return (stream.get("DisplayTitle") or stream.get("Title") or "").lower()


def _filter_by_lang(streams: list, lang_priority: str) -> Optional[list]:
    """Return the first non-empty bucket of streams matching a priority entry."""
    for lang in lang_priority.split(","):
        lang = lang.strip().lower()
        if not lang:
            continue
        bucket = [s for s in streams if (s.get("Language") or "").lower() == lang]
        if bucket:
            return bucket
    return None


def _bias_by_regex(streams: list, pattern: str) -> list:
    pat = re.compile(pattern, re.IGNORECASE)
    preferred = [s for s in streams if pat.search(_track_title(s))]
    return preferred or streams


def _filter_subtype(streams: list, subtype: str) -> list:
    """signs = sign_weight > 0 OR forced; full = neither sign-y nor forced."""
    # Deferred to break a conf -> language_config -> bulk_subtitle -> utils -> conf cycle.
    from .bulk_subtitle import sign_weight

    if subtype == "signs":
        return [
            s for s in streams
            if s.get("IsForced") or sign_weight(_track_title(s)) > 0
        ]
    if subtype == "full":
        return [
            s for s in streams
            if not s.get("IsForced") and sign_weight(_track_title(s)) == 0
        ]
    log.warning("language_config: unknown subtype %r (ignored)", subtype)
    return streams


def _pick_best_sub(streams: list, subtype: Optional[str]):
    """Among candidates, pick the one most appropriate to the requested intent.

    For subtype="signs": lowest sign_weight wins (most clearly a signs track).
    Otherwise: lowest dialogue_weight wins (most clearly a full-dialogue track).
    Ties broken by source order.
    """
    if not streams:
        return None
    from .bulk_subtitle import dialogue_weight, sign_weight

    score = sign_weight if subtype == "signs" else dialogue_weight
    return min(streams, key=lambda s: score(_track_title(s)))


_TYPE_MAP = {"movie": {"Movie"}, "series": {"Episode"}}


def _matches_type(item: dict, rule_type: Optional[str]) -> bool:
    if not rule_type:
        return True
    expected = _TYPE_MAP.get(rule_type.lower())
    if expected is None:
        log.warning("language_config: unknown type %r (rule skipped)", rule_type)
        return False
    return item.get("Type") in expected


def _try_match(rule: LanguageRule, source: dict, item: dict) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """Return (aid, sid) if the rule matches, else None. Either aid or sid may be None."""
    if not _matches_type(item, rule.type):
        return None

    streams = source.get("MediaStreams") or []
    audio = [s for s in streams if s.get("Type") == "Audio"]
    subs = [s for s in streams if s.get("Type") == "Subtitle"]

    # Audio constraints
    if rule.amatch:
        try:
            pat = re.compile(rule.amatch, re.IGNORECASE)
        except re.error:
            log.warning("language_config: invalid amatch regex %r", rule.amatch)
            return None
        audio = [s for s in audio if pat.search(_track_title(s))]
        if not audio:
            return None

    chosen_audio = None
    if rule.alang:
        bucket = _filter_by_lang(audio, rule.alang)
        if bucket is None:
            return None
        if rule.aprefer:
            bucket = _bias_by_regex(bucket, rule.aprefer)
        chosen_audio = bucket[0]
    elif rule.aprefer:
        bucket = _bias_by_regex(audio, rule.aprefer)
        chosen_audio = bucket[0] if bucket else None

    # Subtitle constraints
    if rule.subtype:
        subs = _filter_subtype(subs, rule.subtype)
        if not subs:
            return None
    if rule.smatch:
        try:
            pat = re.compile(rule.smatch, re.IGNORECASE)
        except re.error:
            log.warning("language_config: invalid smatch regex %r", rule.smatch)
            return None
        subs = [s for s in subs if pat.search(_track_title(s))]
        if not subs:
            return None

    chosen_sub = None
    if rule.slang:
        bucket = _filter_by_lang(subs, rule.slang)
        if bucket is None:
            return None
        if rule.sprefer:
            bucket = _bias_by_regex(bucket, rule.sprefer)
        chosen_sub = _pick_best_sub(bucket, rule.subtype)
    elif rule.sprefer:
        bucket = _bias_by_regex(subs, rule.sprefer)
        chosen_sub = _pick_best_sub(bucket, rule.subtype)

    aid = chosen_audio["Index"] if chosen_audio else None
    sid = chosen_sub["Index"] if chosen_sub else None
    return aid, sid


def apply(rules: Optional[List[LanguageRule]], source: dict, item: dict) -> Tuple[Optional[int], Optional[int]]:
    """Walk the rule list; return (aid, sid) from the first matching rule, or (None, None)."""
    if not rules or not source:
        return None, None
    for i, rule in enumerate(rules):
        result = _try_match(rule, source, item)
        if result is not None:
            log.info("matched rule %d -> aid=%s, sid=%s", i + 1, *result)
            return result
    log.info("no rule matched")
    return None, None
