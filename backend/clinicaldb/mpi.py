"""Master Patient Index — one enterprise id per real person, across hospitals.

Every record in the PACS and EHR tables points at an ``mpi_persons.id``.
Hospitals identify the same person differently (their own MRN, a national
id, an insurance number), so a person carries any number of
``patient_identifiers`` rows, each namespaced by a ``system`` URI. Two rules:

1. **Deterministic first.** If any incoming identifier (system + value) is
   already known, that is the person. Identifiers are unique per system, so
   this can never silently join two people.
2. **Probabilistic second.** Otherwise demographics are scored against
   candidates (normalised family/given name, birth date, sex, phone). Above
   ``MPI_AUTO_LINK_SCORE`` the identifiers are attached to the existing
   person; between that and ``MPI_REVIEW_SCORE`` a new person is created and
   a *pending* link is queued for a human to confirm or reject — a wrong
   automatic merge mixes two people's clinical histories, which is far worse
   than a duplicate.

Demographics are stored encrypted (``phi_crypto``) with only the normalised
name tokens, birth date and sex kept in clear for candidate lookup.

Merges (ADT^A40 or a reviewed link) move identifiers to the survivor, mark
the other person ``merged`` and call every registered merge hook so the
PACS/EHR packages can repoint their rows. Reads follow ``merged_into``.
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Callable, Iterable, Optional

import db
import phi_crypto
from clinicaldb import schema  # noqa: F401  (registers tables)
from clinicaldb import settings
from clinicaldb.util import new_id, norm_date, norm_name, norm_sex, row

log = logging.getLogger("clinicaldb.mpi")

_merge_hooks: list[Callable[[Any, str, str], None]] = []


def register_merge_hook(fn: Callable[[Any, str, str], None]) -> None:
    """``fn(conn, survivor_id, merged_id)`` repoints a package's rows."""
    if fn not in _merge_hooks:
        _merge_hooks.append(fn)


# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------
def normalise_demographics(d: dict[str, Any]) -> dict[str, Any]:
    """Accept FHIR-ish or flat input; return the canonical flat shape."""
    d = dict(d or {})
    family = d.get("family")
    given = d.get("given")
    if (not family and not given) and d.get("name"):
        parts = str(d["name"]).replace("^", " ").split()
        if len(parts) == 1:
            family = parts[0]
        elif parts:
            # "Family^Given" (HL7/DICOM) has family first; free text is usually
            # "Given Family". Callers that know which pass family/given.
            if "^" in str(d["name"]):
                family, given = parts[0], " ".join(parts[1:])
            else:
                given, family = " ".join(parts[:-1]), parts[-1]
    if isinstance(given, list):
        given = " ".join(given)
    out = {
        "family": (family or "").strip() or None,
        "given": (given or "").strip() or None,
        "birth_date": norm_date(d.get("birth_date") or d.get("birthDate") or d.get("dob")),
        "sex": norm_sex(d.get("sex") or d.get("gender")),
        "phone": (d.get("phone") or "").strip() or None,
        "address": d.get("address") or None,
        "email": d.get("email") or None,
        "language": d.get("language") or None,
        "deceased": bool(d.get("deceased")) if d.get("deceased") is not None else None,
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def display_name(demo: dict[str, Any]) -> str:
    return " ".join(x for x in (demo.get("given"), demo.get("family")) if x) or "(unnamed)"


def _digits(s: Optional[str]) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def score(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Similarity in [0, 1]; *b*'s recorded name aliases are also tried."""
    best = _score_one(a, b)
    for alias in b.get("aliases") or []:
        best = max(best, _score_one(a, {**b, **alias}))
    return best


def _score_one(a: dict[str, Any], b: dict[str, Any]) -> float:
    total = 0.0

    def sim(x: Optional[str], y: Optional[str]) -> float:
        x, y = norm_name(x), norm_name(y)
        if not x or not y:
            return 0.0
        if x == y:
            return 1.0
        r = difflib.SequenceMatcher(None, x, y).ratio()
        return r if r >= 0.8 else 0.0

    total += 0.35 * sim(a.get("family"), b.get("family"))
    total += 0.25 * sim(a.get("given"), b.get("given"))
    da, dbb = a.get("birth_date"), b.get("birth_date")
    if da and dbb:
        if da == dbb:
            total += 0.25
        elif da[:4] == dbb[:4]:
            total += 0.08
        else:
            total -= 0.15
    sa, sb = a.get("sex"), b.get("sex")
    if sa and sb and "unknown" not in (sa, sb):
        total += 0.05 if sa == sb else -0.2
    pa, pb = _digits(a.get("phone")), _digits(b.get("phone"))
    if pa and pb and pa == pb:
        total += 0.10
    return max(0.0, min(1.0, round(total, 4)))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def resolve(person_id: str, conn=None) -> Optional[str]:
    """Follow ``merged_into`` to the surviving person id."""
    seen: set[str] = set()
    own = conn is None
    c = db.connect() if own else conn
    try:
        pid = person_id
        while pid and pid not in seen:
            seen.add(pid)
            r = c.execute("SELECT id, merged_into FROM mpi_persons WHERE id=?",
                          (pid,)).fetchone()
            if r is None:
                return None
            if not r["merged_into"]:
                return r["id"]
            pid = r["merged_into"]
        return None
    finally:
        if own:
            c.close()


def _decode(r) -> dict:
    d = row(r)
    d["demographics"] = phi_crypto.decrypt_json(d["demographics"], patient_id=d["id"],
                                                owner_user_id=None) or {}
    return d


def get(person_id: str, *, follow: bool = True) -> Optional[dict]:
    with db.connect() as c:
        pid = resolve(person_id, c) if follow else person_id
        if not pid:
            return None
        r = c.execute("SELECT * FROM mpi_persons WHERE id=?", (pid,)).fetchone()
        if r is None:
            return None
        p = _decode(r)
        p["identifiers"] = [row(x) for x in c.execute(
            "SELECT system, value, type, facility_oid FROM patient_identifiers "
            "WHERE person_id=? ORDER BY created_at", (pid,)).fetchall()]
    p["name"] = display_name(p["demographics"])
    return p


def find_by_identifier(system: str, value: str) -> Optional[str]:
    with db.connect() as c:
        r = c.execute("SELECT person_id FROM patient_identifiers WHERE system=? AND value=?",
                      (system, str(value).strip())).fetchone()
        return resolve(r["person_id"], c) if r else None


def find_by_any_identifier(identifiers: Iterable[dict]) -> Optional[str]:
    for ident in identifiers or []:
        if ident.get("system") and ident.get("value"):
            pid = find_by_identifier(ident["system"], ident["value"])
            if pid:
                return pid
    return None


def local_mrn(person_id: str, facility_oid: Optional[str] = None) -> Optional[str]:
    system = settings.mrn_system(facility_oid)
    with db.connect() as c:
        r = c.execute("SELECT value FROM patient_identifiers WHERE person_id=? AND system=?",
                      (person_id, system)).fetchone()
        return r["value"] if r else None


def candidates(demo: dict[str, Any], *, limit: int = 25) -> list[dict]:
    fam, giv, dob = norm_name(demo.get("family")), norm_name(demo.get("given")), demo.get("birth_date")
    clauses, params = [], []
    if fam:
        clauses.append("family_norm = ?")
        params.append(fam)
    if dob:
        clauses.append("birth_date = ?")
        params.append(dob)
    if giv:
        clauses.append("given_norm = ?")
        params.append(giv)
    if not clauses:
        return []
    sql = ("SELECT * FROM mpi_persons WHERE status='active' AND (" + " OR ".join(clauses)
           + ") LIMIT ?")
    with db.connect() as c:
        rows = c.execute(sql, (*params, limit * 4)).fetchall()
    scored = []
    for r in rows:
        p = _decode(r)
        s = score(demo, p["demographics"])
        if s > 0:
            scored.append({"person_id": p["id"], "score": s,
                           "name": display_name(p["demographics"]),
                           "demographics": p["demographics"]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def search(*, name: Optional[str] = None, family: Optional[str] = None,
           given: Optional[str] = None, birth_date: Optional[str] = None,
           identifier: Optional[str] = None, sex: Optional[str] = None,
           limit: int = 50) -> list[dict]:
    """Search people by identifier value, name fragment and/or birth date."""
    out: dict[str, dict] = {}
    with db.connect() as c:
        if identifier:
            val = identifier.split("|", 1)[-1].strip()
            sys_ = identifier.split("|", 1)[0].strip() if "|" in identifier else None
            q = "SELECT person_id FROM patient_identifiers WHERE value=?"
            p: list = [val]
            if sys_:
                q += " AND system=?"
                p.append(sys_)
            for r in c.execute(q, p).fetchall():
                pid = resolve(r["person_id"], c)
                if pid:
                    out[pid] = {"person_id": pid, "score": 1.0}
        clauses, params = ["status='active'"], []
        tokens = norm_name(" ".join(x for x in (name, family, given) if x)).split()
        for t in tokens:
            clauses.append("name_tokens LIKE ?")
            params.append(f"%{t}%")
        if birth_date:
            clauses.append("birth_date = ?")
            params.append(norm_date(birth_date))
        if sex:
            clauses.append("sex = ?")
            params.append(norm_sex(sex))
        if len(clauses) > 1:
            for r in c.execute("SELECT id FROM mpi_persons WHERE " + " AND ".join(clauses)
                               + " ORDER BY updated_at DESC LIMIT ?", (*params, limit)).fetchall():
                out.setdefault(r["id"], {"person_id": r["id"], "score": 0.9})
    results = []
    for pid, hit in list(out.items())[:limit]:
        p = get(pid)
        if p:
            results.append({**hit, "name": p["name"], "demographics": p["demographics"],
                            "identifiers": p["identifiers"],
                            "home_facility": p.get("home_facility")})
    return results


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _write_person(c, pid: str, demo: dict, home_facility: Optional[str], *, insert: bool) -> None:
    now = db.now()
    enc = phi_crypto.encrypt_json(demo, patient_id=pid, owner_user_id=None)
    names = [demo] + list(demo.get("aliases") or [])
    tokens = sorted({t for n in names for k in ("family", "given")
                     for t in norm_name(n.get(k)).split()})
    vals = (norm_name(demo.get("family")) or None, norm_name(demo.get("given")) or None,
            demo.get("birth_date"), demo.get("sex"), enc, " " + " ".join(tokens) + " ")
    if insert:
        c.execute("INSERT INTO mpi_persons(id, family_norm, given_norm, birth_date, sex, "
                  "demographics, name_tokens, home_facility, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, *vals, home_facility, now, now))
    else:
        c.execute("UPDATE mpi_persons SET family_norm=?, given_norm=?, birth_date=?, sex=?, "
                  "demographics=?, name_tokens=?, updated_at=? WHERE id=?", (*vals, now, pid))


def add_identifier(person_id: str, system: str, value: str, *, type_: str = "MR",
                   facility_oid: Optional[str] = None, conn=None) -> bool:
    """Attach an identifier. Returns False if it already belongs to someone."""
    value = str(value).strip()
    if not system or not value:
        return False
    own = conn is None
    c = db.connect() if own else conn
    try:
        r = c.execute("SELECT person_id FROM patient_identifiers WHERE system=? AND value=?",
                      (system, value)).fetchone()
        if r:
            return resolve(r["person_id"], c) == resolve(person_id, c)
        c.execute("INSERT INTO patient_identifiers(id, person_id, system, value, type, "
                  "facility_oid, created_at) VALUES (?,?,?,?,?,?,?)",
                  (new_id(), person_id, system, value, type_, facility_oid, db.now()))
        return True
    finally:
        if own:
            c.close()


def update_demographics(person_id: str, demographics: dict[str, Any]) -> Optional[dict]:
    """Merge *demographics* over the stored ones (newer non-empty values win)."""
    p = get(person_id)
    if not p:
        return None
    merged = {**p["demographics"], **normalise_demographics(demographics)}
    with db.connect() as c:
        _write_person(c, p["id"], merged, p.get("home_facility"), insert=False)
    return get(p["id"])


def register_person(demographics: dict[str, Any], identifiers: Iterable[dict] = (), *,
                    source_facility: Optional[str] = None,
                    update_existing: bool = True) -> dict:
    """Match-or-create. Returns ``{person_id, outcome, score, review_link}``.

    ``outcome`` is one of ``matched_identifier``, ``matched_demographics``,
    ``created`` or ``created_pending_review``.
    """
    demo = normalise_demographics(demographics)
    idents = [i for i in identifiers or [] if i.get("system") and i.get("value")]
    facility = source_facility or settings.facility_oid()

    pid = find_by_any_identifier(idents)
    outcome, best_score, review_link = None, None, None
    if pid:
        outcome, best_score = "matched_identifier", 1.0
    else:
        cands = candidates(demo) if demo else []
        if cands and cands[0]["score"] >= settings.mpi_auto_link_score():
            pid, outcome, best_score = cands[0]["person_id"], "matched_demographics", cands[0]["score"]
        elif cands and cands[0]["score"] >= settings.mpi_review_score():
            best_score = cands[0]["score"]
            review_link = cands[0]["person_id"]

    with db.connect() as c:
        if pid is None:
            pid = new_id()
            _write_person(c, pid, demo, facility, insert=True)
            outcome = "created_pending_review" if review_link else "created"
            if review_link:
                c.execute("INSERT INTO mpi_links(id, person_id, other_id, kind, score, status, "
                          "reason, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (new_id(), pid, review_link, "possible_duplicate", best_score,
                           "pending", "demographic score between review and auto-link",
                           db.now()))
        elif update_existing and demo:
            # The golden record is not overwritten by whichever system spoke
            # last: incoming values only fill gaps, and a differently spelled
            # name (another script, a transliteration) is kept as an alias so
            # future matching recognises it too.
            r = c.execute("SELECT * FROM mpi_persons WHERE id=?", (pid,)).fetchone()
            current = _decode(r)["demographics"]
            merged = {**demo, **current}
            merged.pop("aliases", None)
            aliases = list(current.get("aliases") or [])
            incoming = {k: demo[k] for k in ("family", "given") if demo.get(k)}
            known = [{k: current.get(k) for k in ("family", "given")}] + aliases
            if incoming and not any(
                    all(norm_name(x.get(k)) == norm_name(v) for k, v in incoming.items())
                    for x in known):
                aliases.append(incoming)
            if aliases:
                merged["aliases"] = aliases
            if merged != current:
                _write_person(c, pid, merged, None, insert=False)
        for ident in idents:
            add_identifier(pid, ident["system"], ident["value"],
                           type_=ident.get("type") or "MR",
                           facility_oid=ident.get("facility_oid"), conn=c)
    return {"person_id": pid, "outcome": outcome, "score": best_score,
            "review_link": review_link}


def ensure_local_mrn(person_id: str, mrn: Optional[str] = None) -> str:
    """Return this facility's MRN for the person, issuing one if absent."""
    existing = local_mrn(person_id)
    if existing:
        return existing
    value = (mrn or "").strip() or ("MRN-" + person_id[:10].upper())
    add_identifier(person_id, settings.mrn_system(), value, type_="MR",
                   facility_oid=settings.facility_oid())
    return value


def merge(survivor_id: str, merged_id: str, *, reason: str = "",
          actor: Optional[str] = None) -> dict:
    """Merge *merged_id* into *survivor_id* (identifiers, rows, links)."""
    with db.connect() as c:
        s = resolve(survivor_id, c)
        m = resolve(merged_id, c)
        if not s or not m:
            raise ValueError("unknown person")
        if s == m:
            return {"survivor": s, "merged": m, "changed": False}
        db.begin_immediate(c)
        try:
            c.execute("UPDATE patient_identifiers SET person_id=? WHERE person_id=?", (s, m))
            c.execute("UPDATE mpi_persons SET status='merged', merged_into=?, updated_at=? "
                      "WHERE id=?", (s, db.now(), m))
            c.execute("UPDATE mpi_links SET status='resolved', resolved_at=? "
                      "WHERE status='pending' AND ((person_id=? AND other_id=?) OR "
                      "(person_id=? AND other_id=?))", (db.now(), s, m, m, s))
            c.execute("INSERT INTO mpi_links(id, person_id, other_id, kind, score, status, "
                      "reason, created_by, created_at, resolved_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (new_id(), s, m, "merge", 1.0, "applied", reason or None, actor,
                       db.now(), db.now()))
            for hook in _merge_hooks:
                hook(c, s, m)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return {"survivor": s, "merged": m, "changed": True}


def pending_links(limit: int = 100) -> list[dict]:
    with db.connect() as c:
        return [row(r) for r in c.execute(
            "SELECT * FROM mpi_links WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()]


def resolve_link(link_id: str, *, accept: bool, actor: Optional[str] = None) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT * FROM mpi_links WHERE id=?", (link_id,)).fetchone()
    if not r:
        raise ValueError("unknown link")
    link = row(r)
    if link["status"] != "pending":
        raise ValueError("link already resolved")
    if accept:
        # The older record survives: it is the one other systems already reference.
        merge(link["other_id"], link["person_id"], reason="reviewed duplicate", actor=actor)
    else:
        with db.connect() as c:
            c.execute("UPDATE mpi_links SET status='rejected', resolved_at=?, created_by=? "
                      "WHERE id=?", (db.now(), actor, link_id))
    return {"link_id": link_id, "accepted": accept}
