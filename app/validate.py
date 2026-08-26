"""
The grounding validator.

Every other Bible-AI product asks the model nicely to cite its sources. This
checks. Claims whose citations don't resolve to something actually retrieved are
removed before the user ever sees them, and the removal is counted and reported.

Three things this catches that a prompt alone will not:

1. Fabricated sigla. The model invents `MHC:9999` for a plausible-sounding
   commentary observation. Caught: 9999 isn't in the packet.
2. Citation drift. A real siglum attached to a claim it doesn't support. Caught
   partially, by the lexical overlap check below — imperfect, but it flags the
   worst cases.
3. Structural cheating on `disputed`. A "disputed" entry with one real position
   and one strawman, or with positions attributed to "some people". Caught by
   the minimum-positions and named-holder checks.

The grounding score is shown in the UI. A user who sees "4 claims removed as
uncited" learns something true about how much to trust the rest.
"""
import re
import unicodedata
from dataclasses import dataclass, field

VAGUE_HOLDERS = re.compile(
    r"^\s*(some|many|most|certain|various|other"
    r"|algunos|muchos|ciertos|varios|otros|la mayoría|la mayoria)?\s*"
    r"(christians?|people|scholars?|believers?|interpreters?|theologians?|commentators?"
    r"|cristianos?|personas|gente|eruditos?|creyentes?|int[eé]rpretes?|te[oó]logos?"
    r"|comentaristas?)\s*$",
    re.I,
)

# Words too common to signal that a citation actually supports a claim.
STOP = set("""the a an and or but of to in on at for with by from as is are was were
be been being this that these those it its his her their they them we us you your
which who whom what when where how not no nor if then than so such all any both each
have has had do does did will would shall should may might can could must about into
through during before after above below up down out off over under again further
one two first second god lord jesus christ
que los las una unas unos con por para como sin sobre entre desde hasta cuando donde
pero porque aunque este esta esto esos esas aquel aquella cual cuales quien quienes
ser son era eran sido siendo estar esta estan haber hay habia tiene tienen tener
todo toda todos todas otro otra otros otras mismo misma su sus nos les del
dios senor jesus cristo""".split())


@dataclass
class Report:
    removed: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_claims: int = 0
    grounded_claims: int = 0

    @property
    def score(self) -> float:
        if not self.total_claims:
            return 1.0
        return round(self.grounded_claims / self.total_claims, 3)

    def to_dict(self):
        return {
            "grounding_score": self.score,
            "claims_checked": self.total_claims,
            "claims_removed": len(self.removed),
            "removed": self.removed,
            "warnings": self.warnings,
        }


def _tokens(s: str) -> set[str]:
    """
    Content words, accent-folded so 'salvación' and 'salvacion' match. Folding
    also means Spanish claims can be checked against Spanish sources without the
    accent inconsistencies in older public-domain texts causing false misses.
    """
    folded = unicodedata.normalize("NFD", (s or "").lower())
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
    return {w for w in re.findall(r"[a-z]{4,}", folded)} - STOP


def _support_index(evidence: dict) -> dict[str, set[str]]:
    """siglum -> content words available under it, for the drift check."""
    idx = {}
    for v in evidence.get("passage", []):
        idx[v["siglum"]] = _tokens(v.get("text", ""))
    for w in evidence.get("words", []):
        idx[w["siglum"]] = _tokens(
            " ".join([w.get("definition", ""), w.get("kjv_usage", ""),
                      w.get("derivation", ""), w.get("translit", ""),
                      w.get("surface", "")])
        )
    for x in evidence.get("cross_refs", []):
        idx[x["siglum"]] = _tokens(x.get("text", "") + " " + x.get("ref", ""))
    for c in evidence.get("commentary", []):
        idx[c["siglum"]] = _tokens(c.get("text", ""))
    return idx


def _check(item: dict, text_fields: list[str], allowed: set[str],
           support: dict[str, set[str]], path: str, rep: Report,
           require_cites: bool = True, check_drift: bool = True) -> bool:
    """Returns True if the item is adequately grounded."""
    rep.total_claims += 1
    cites = [c for c in (item.get("cites") or []) if isinstance(c, str)]
    claim_text = " ".join(str(item.get(f, "")) for f in text_fields)

    if require_cites and not cites:
        rep.removed.append({"path": path, "reason": "no citation", "claim": claim_text[:220]})
        return False

    unknown = [c for c in cites if c not in allowed]
    resolved = [c for c in cites if c in allowed]

    if unknown and not resolved:
        rep.removed.append({
            "path": path,
            "reason": f"citations not in evidence: {', '.join(unknown[:4])}",
            "claim": claim_text[:220],
        })
        return False

    if unknown:
        rep.warnings.append(f"{path}: dropped unresolvable siglum {', '.join(unknown[:3])}")
        item["cites"] = resolved

    # Drift check: does any cited source share vocabulary with the claim?
    # Deliberately lenient -- one overlapping content word passes -- because the
    # goal is catching wholesale fabrication, not policing paraphrase.
    if resolved and check_drift:
        claim_tokens = _tokens(claim_text)
        if claim_tokens:
            pooled = set()
            for c in resolved:
                pooled |= support.get(c, set())
            if pooled and not (claim_tokens & pooled):
                rep.warnings.append(
                    f"{path}: claim shares no vocabulary with its sources — possible citation drift"
                )

    rep.grounded_claims += 1
    return True


def validate(study: dict, evidence: dict, lang: str = "en") -> tuple[dict, Report]:
    """
    Strip ungrounded content. Returns (cleaned_study, report).

    `lang` is the language the study was written in. When it differs from the
    language of the sources -- a Spanish study citing English commentary, which
    is the normal case since public-domain Spanish commentary is scarce -- the
    lexical drift heuristic is switched off. It compares words, and translated
    words never match, so leaving it on would fire on every single claim and
    train you to ignore the warnings.

    The check that actually matters is unaffected: citations must still resolve
    to sigla present in the evidence, and fabricated ones are still deleted.
    Only the weaker "does this claim share vocabulary with its source" heuristic
    is lost, and the report says so rather than quietly degrading.
    """
    rep = Report()
    source_langs = {c.get("source_lang", "en") for c in evidence.get("commentary", [])}
    check_drift = not source_langs or source_langs == {lang}
    if not check_drift:
        rep.warnings.append(
            "cross-language study: citation existence enforced, "
            "vocabulary-overlap check disabled"
        )
    allowed = set()
    for group in ("passage", "words", "cross_refs", "commentary"):
        for item in evidence.get(group, []):
            allowed.add(item["siglum"])
    support = _support_index(evidence)

    study.setdefault("insufficient_evidence", [])

    # --- settled ---
    kept = []
    for i, c in enumerate(study.get("settled") or []):
        if not isinstance(c, dict):
            continue
        if c.get("certainty") not in ("explicit", "strongly_implied", "inferred"):
            c["certainty"] = "inferred"   # unlabelled defaults to weakest
        if _check(c, ["claim"], allowed, support, f"settled[{i}]", rep, check_drift=check_drift):
            kept.append(c)
    study["settled"] = kept

    # --- key_terms ---
    kept = []
    for i, t in enumerate(study.get("key_terms") or []):
        if not isinstance(t, dict):
            continue
        s = (t.get("strongs") or "").strip()
        if s and s not in allowed:
            rep.removed.append({
                "path": f"key_terms[{i}]",
                "reason": f"Strong's entry {s} not retrieved for this passage",
                "claim": t.get("surface", ""),
            })
            rep.total_claims += 1
            continue
        if _check(t, ["why_it_matters", "gloss"], allowed, support, f"key_terms[{i}]", rep, check_drift=check_drift):
            kept.append(t)
    study["key_terms"] = kept

    # --- connections ---
    kept = []
    for i, c in enumerate(study.get("connections") or []):
        if not isinstance(c, dict):
            continue
        if _check(c, ["relation"], allowed, support, f"connections[{i}]", rep, check_drift=check_drift):
            kept.append(c)
    study["connections"] = kept

    # --- disputed: structural integrity matters more than citations here ---
    kept = []
    for i, d in enumerate(study.get("disputed") or []):
        if not isinstance(d, dict):
            continue
        rep.total_claims += 1
        positions = [p for p in (d.get("positions") or []) if isinstance(p, dict)]

        if len(positions) < 2:
            rep.removed.append({
                "path": f"disputed[{i}]",
                "reason": "a disputed question needs at least two positions",
                "claim": d.get("question", "")[:220],
            })
            continue

        vague = [p for p in positions if VAGUE_HOLDERS.match(p.get("held_by", ""))]
        if vague:
            rep.warnings.append(
                f"disputed[{i}]: position attributed vaguely "
                f"({vague[0].get('held_by')!r}) — named traditions required"
            )

        # Lopsided reasoning is how a "balanced" section quietly picks a winner.
        lengths = [len(p.get("reasoning", "")) for p in positions]
        if lengths and max(lengths) > 3 * max(min(lengths), 1):
            rep.warnings.append(
                f"disputed[{i}]: one position argued far more fully than another — "
                "check for a strawman"
            )

        d["positions"] = positions
        rep.grounded_claims += 1
        kept.append(d)
    study["disputed"] = kept

    # --- misreadings ---
    kept = []
    for i, m in enumerate(study.get("common_misreadings") or []):
        if not isinstance(m, dict):
            continue
        if _check(m, ["correction"], allowed, support,
                  f"common_misreadings[{i}]", rep, require_cites=False,
                  check_drift=check_drift):
            kept.append(m)
    study["common_misreadings"] = kept

    # A study on a historically contested passage with an empty disputed array
    # is a signal worth surfacing to you in logs, even if it's sometimes fine.
    if not study["disputed"] and len(study["settled"]) > 6:
        rep.warnings.append(
            "many settled claims and no disputed questions — verify the passage "
            "is genuinely uncontested"
        )

    return study, rep


def validate_topic(study: dict, evidence: dict, lang: str = "en") -> tuple[dict, Report]:
    """
    Same grounding discipline applied to a topical study.

    One extra structural check: `commonly_cited_but_does_not_say_this` may only
    name passages that were actually retrieved. Without that, the model can
    invent a misreading of a verse it never saw -- which would be its own kind
    of confident error, in the one section meant to prevent them.
    """
    rep = Report()
    allowed = set()
    for group in ("passage", "words", "cross_refs", "commentary"):
        for item in evidence.get(group, []):
            allowed.add(item["siglum"])
    support = _support_index(evidence)

    source_langs = {c.get("source_lang", "en") for c in evidence.get("commentary", [])}
    check_drift = not source_langs or source_langs == {lang}
    if not check_drift:
        rep.warnings.append("cross-language study: vocabulary-overlap check disabled")

    study.setdefault("insufficient_evidence", [])
    study.setdefault("where_scripture_is_quiet", [])

    for key, fields in (("what_scripture_addresses", ["claim"]),
                        ("key_passages", ["what_it_says"]),
                        ("key_terms", ["why_it_matters", "gloss"])):
        kept = []
        for i, item in enumerate(study.get(key) or []):
            if not isinstance(item, dict):
                continue
            if key == "what_scripture_addresses" and item.get("certainty") not in (
                    "explicit", "strongly_implied", "inferred"):
                item["certainty"] = "inferred"
            if key == "key_terms":
                sid = (item.get("strongs") or "").strip()
                if sid and sid not in allowed:
                    rep.total_claims += 1
                    rep.removed.append({"path": f"{key}[{i}]",
                                        "reason": f"lexicon entry {sid} not retrieved",
                                        "claim": item.get("surface", "")})
                    continue
            if _check(item, fields, allowed, support, f"{key}[{i}]", rep,
                      check_drift=check_drift):
                kept.append(item)
        study[key] = kept

    kept = []
    for i, m in enumerate(study.get("commonly_cited_but_does_not_say_this") or []):
        if not isinstance(m, dict):
            continue
        if _check(m, ["what_it_actually_says", "popular_use"], allowed, support,
                  f"misused[{i}]", rep, check_drift=check_drift):
            kept.append(m)
    study["commonly_cited_but_does_not_say_this"] = kept

    kept = []
    for i, p in enumerate(study.get("positions") or []):
        if not isinstance(p, dict):
            continue
        if VAGUE_HOLDERS.match(p.get("held_by", "")):
            rep.warnings.append(
                f"positions[{i}]: vague attribution ({p.get('held_by')!r})")
        kept.append(p)
    if len(kept) == 1:
        rep.warnings.append(
            "only one position given — either the topic is uncontested, or a "
            "second view was dropped")
    study["positions"] = kept

    return study, rep


def validate_sermon(study: dict, evidence: dict, lang: str = "en") -> tuple[dict, Report]:
    """
    Grounding checks for the preaching workbench, plus two structural rules the
    schema alone can't enforce.

    The first is the important one. `application_prompts` and
    `illustration_prompts` are supposed to be QUESTIONS that help a preacher
    find his own material. Models drift toward being helpful and start writing
    the application instead — "Remind your congregation that God is faithful
    even when work is hard." That is a sermon written by something that has
    never met the congregation, and it is the exact failure this feature was
    designed around. Declarative sentences in those fields get flagged.
    """
    rep = Report()
    allowed = set()
    for group in ("passage", "words", "cross_refs", "commentary"):
        for item in evidence.get(group, []):
            allowed.add(item["siglum"])
    support = _support_index(evidence)

    source_langs = {c.get("source_lang", "en") for c in evidence.get("commentary", [])}
    check_drift = not source_langs or source_langs == {lang}
    if not check_drift:
        rep.warnings.append("cross-language: vocabulary-overlap check disabled")

    study.setdefault("insufficient_evidence", [])

    kept = []
    for i, m in enumerate(study.get("movements") or []):
        if not isinstance(m, dict):
            continue
        if _check(m, ["the_point", "title"], allowed, support,
                  f"movements[{i}]", rep, check_drift=check_drift):
            m["illustration_prompts"] = _prompts_only(
                m.get("illustration_prompts"), f"movements[{i}].illustration_prompts", rep)
            _craft_only(m, "delivery", f"movements[{i}].delivery", rep)
            _craft_only(m, "transition_cue", f"movements[{i}].transition_cue", rep, max_len=200)
            kept.append(m)
    study["movements"] = kept

    # Timing has to be usable. An outline that runs 40 minutes in a 20-minute
    # slot is worse than no outline, because the preacher discovers it live.
    target = int(study.get("_target_minutes") or 0)
    mins = [int(m.get("minutes") or 0) for m in study["movements"]]
    total = sum(mins)
    if total:
        study["_movement_minutes"] = total
        if target and abs((total + 4) - target) > max(5, target * 0.3):
            rep.warnings.append(
                f"timings total {total} min of content for a {target} min slot — "
                f"check the pacing before you rely on it")
    study.pop("_target_minutes", None)

    for i, w in enumerate(study.get("where_they_lose_you") or []):
        if isinstance(w, dict):
            _craft_only(w, "what_helps", f"where_they_lose_you[{i}].what_helps", rep)

    kept = []
    for i, d in enumerate(study.get("do_not_preach") or []):
        if not isinstance(d, dict):
            continue
        if _check(d, ["why_not", "claim"], allowed, support,
                  f"do_not_preach[{i}]", rep, require_cites=False,
                  check_drift=check_drift):
            kept.append(d)
    study["do_not_preach"] = kept

    # Decisions must stay open. One option is not a decision.
    kept = []
    for i, d in enumerate(study.get("decisions_to_make") or []):
        if not isinstance(d, dict):
            continue
        rep.total_claims += 1
        opts = [o for o in (d.get("options") or []) if isinstance(o, dict)]
        if len(opts) < 2:
            rep.removed.append({
                "path": f"decisions_to_make[{i}]",
                "reason": "a decision needs at least two options",
                "claim": d.get("question", "")[:200],
            })
            continue
        for o in opts:
            if VAGUE_HOLDERS.match(o.get("held_by", "")):
                rep.warnings.append(
                    f"decisions_to_make[{i}]: vague attribution ({o.get('held_by')!r})")
        d["options"] = opts
        rep.grounded_claims += 1
        kept.append(d)
    study["decisions_to_make"] = kept

    study["application_prompts"] = _prompts_only(
        study.get("application_prompts"), "application_prompts", rep)

    return study, rep


_QUESTION_ENDINGS = ("?", "？")
_IMPERATIVE_LEAD = re.compile(
    r"^\s*(remind|tell|encourage|urge|challenge|call|invite|exhort|assure|"
    r"point out to|help them see|show them|recuerda|dile|anima|exhorta)\b", re.I)


# Craft notes are addressed to the preacher about how to speak. The failure
# mode is the same one _prompts_only guards: the model slides from "slow down
# on verse 4" to "tell them God is near." Congregation-facing imperatives are
# the tell.
_CONGREGATION = re.compile(
    r"\b(remind|tell|assure|comfort|exhort|urge|challenge)\s+(them|your\s+"
    r"(congregation|people|church|listeners|hearers))\b", re.I)


def _craft_only(obj: dict, field: str, path: str, rep: Report, max_len: int = 400):
    """Strip a delivery note that has turned into content."""
    v = obj.get(field)
    if not isinstance(v, str) or not v.strip():
        obj.pop(field, None)
        return
    v = v.strip()
    if _CONGREGATION.search(v):
        rep.warnings.append(f"{path}: content, not craft — removed")
        obj.pop(field, None)
        return
    if len(v) > max_len:
        rep.warnings.append(f"{path}: unusually long for a delivery note — check it")
    obj[field] = v


def _prompts_only(items, path: str, rep: Report) -> list:
    """
    Keep prompts, drop written application.

    A question mark is the cheap reliable signal. An imperative aimed at the
    congregation ("Remind your people that...") is the failure mode by name.
    """
    out = []
    for i, s in enumerate(items or []):
        if not isinstance(s, str) or not s.strip():
            continue
        s = s.strip()
        if _IMPERATIVE_LEAD.match(s):
            rep.warnings.append(
                f"{path}[{i}]: written application, not a prompt — removed")
            continue
        if not s.endswith(_QUESTION_ENDINGS):
            rep.warnings.append(
                f"{path}[{i}]: stated rather than asked — check it is a prompt")
        out.append(s)
    return out
