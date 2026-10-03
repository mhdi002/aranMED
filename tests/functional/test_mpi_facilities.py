"""Functional: facility registry + Master Patient Index over the HTTP API.

Walks the identity lifecycle a hospital network actually goes through:
register at one site, arrive at another with a different MRN, arrive again
under a Persian spelling variant, a near-miss that needs human review, the
reviewed merge, and the audit trail all of that leaves behind.
"""
from __future__ import annotations

import base64
import os

import db


def test_capabilities_reports_identity_and_schema(client):
    r = client.get("/api/interop/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["facility"]["oid"] == "2.25.1001"
    assert body["facility"]["name"] == "Test General Hospital"
    assert body["facility"]["ae_title"] == "TESTPACS"
    assert body["schema"]["mpi"] >= 1
    assert body["schema"] == {k: v for k, v in body["schema"].items()}
    assert body["schema"]["mpi"] == body["schema_expected"]["mpi"]
    assert body["identifier_systems"]["mrn"] == "urn:oid:2.25.1001"


def test_facility_registry_admin_flow(client, users):
    peer = {"oid": "2.25.2002", "name": "Peer Hospital", "kind": "hospital",
            "fhir_base": "http://peer.invalid/fhir", "dicomweb_base": "http://peer.invalid/dicom-web",
            "ae_title": "PEERPACS", "dicom_host": "peer.invalid", "dicom_port": 11112,
            "secret_env": "PEER_2002_SECRET"}
    # Doctors can read the registry but not change it.
    assert client.post("/api/facilities", json=peer, headers=users["doctor"]).status_code == 403
    r = client.post("/api/facilities", json=peer, headers=users["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["has_secret"] is False

    os.environ["PEER_2002_SECRET"] = "s3cret"
    try:
        listing = client.get("/api/facilities", headers=users["doctor"]).json()["facilities"]
        oids = [f["oid"] for f in listing]
        assert oids[0] == "2.25.1001"  # local first
        assert "2.25.2002" in oids
        peer_row = next(f for f in listing if f["oid"] == "2.25.2002")
        assert peer_row["has_secret"] is True
        assert "s3cret" not in r.text  # the value itself is never stored or echoed
    finally:
        del os.environ["PEER_2002_SECRET"]

    # Update in place (same OID), then the local facility is protected.
    r = client.post("/api/facilities", json={**peer, "name": "Peer Hospital (North)"},
                    headers=users["admin"])
    assert r.json()["name"] == "Peer Hospital (North)"
    r = client.post("/api/facilities", json={"oid": "2.25.1001", "name": "Hijack"},
                    headers=users["admin"])
    assert r.status_code == 400
    assert client.delete("/api/facilities/2.25.1001", headers=users["admin"]).status_code == 404
    assert client.delete("/api/facilities/2.25.2002", headers=users["admin"]).status_code == 200


def test_mpi_identity_lifecycle(client, users):
    doc = users["doctor"]
    # 1. First registration at this hospital.
    r = client.post("/api/mpi/register", headers=doc, json={
        "demographics": {"family": "کریمی", "given": "علی", "birth_date": "1980-04-12",
                         "sex": "male", "phone": "+98 912 000 1111"},
        "national_id": "0012345678", "mrn": "TGH-1"})
    assert r.status_code == 200, r.text
    first = r.json()
    pid = first["person_id"]
    assert first["outcome"] == "created"
    assert first["mrn"] == "TGH-1"

    # 2. Same person registered with another hospital's MRN + the national id:
    #    deterministic identifier match, identifiers accumulate.
    r = client.post("/api/mpi/register", headers=doc, json={
        "demographics": {"family": "Karimi", "given": "Ali", "birth_date": "19800412"},
        "identifiers": [{"system": "urn:oid:2.25.2002", "value": "PH-77", "type": "MR",
                         "facility_oid": "2.25.2002"},
                        {"system": "urn:aranmed:national-id", "value": "0012345678",
                         "type": "NI"}]})
    assert r.json()["outcome"] == "matched_identifier"
    assert r.json()["person_id"] == pid
    person = client.get(f"/api/mpi/{pid}", headers=doc).json()
    systems = {i["system"] for i in person["identifiers"]}
    assert {"urn:oid:2.25.1001", "urn:oid:2.25.2002", "urn:aranmed:national-id"} <= systems

    # 3. No identifiers, Arabic-script spelling variants (ي/ك) of the same name,
    #    same birth date and phone: high demographic score -> auto-link.
    r = client.post("/api/mpi/register", headers=doc, json={
        "demographics": {"family": "كريمي", "given": "علي", "birth_date": "1980-04-12",
                         "sex": "male", "phone": "09120001111"}})
    assert r.json()["outcome"] == "matched_demographics", r.json()
    assert r.json()["person_id"] == pid

    # 4. Same family, a one-letter-off given name, same birth date, no phone:
    #    plausible duplicate (score between review and auto-link) -> a new
    #    person is created and a link is queued for human review.
    r = client.post("/api/mpi/register", headers=doc, json={
        "demographics": {"family": "Karimi", "given": "Alii", "birth_date": "1980-04-12",
                         "sex": "male"},
        "mrn": "TGH-2"})
    second = r.json()
    assert second["outcome"] == "created_pending_review", second
    assert 0.75 <= second["score"] < 0.92
    assert second["person_id"] != pid
    links = client.get("/api/mpi/links", headers=doc).json()["links"]
    link = next(l for l in links if l["person_id"] == second["person_id"])
    assert link["other_id"] == pid

    # 5. Search by name fragment and by identifier.
    hits = client.get("/api/mpi/search", params={"q": "karimi"}, headers=doc).json()["results"]
    assert {h["person_id"] for h in hits} >= {pid, second["person_id"]}
    hits = client.get("/api/mpi/search", params={"identifier": "PH-77"},
                      headers=doc).json()["results"]
    assert [h["person_id"] for h in hits] == [pid]

    # 6. Reviewer accepts: the older record survives, the duplicate redirects.
    r = client.post(f"/api/mpi/links/{link['id']}/resolve", params={"accept": True},
                    headers=doc)
    assert r.status_code == 200
    redirected = client.get(f"/api/mpi/{second['person_id']}", headers=doc).json()
    assert redirected["id"] == pid
    assert "TGH-2" in {i["value"] for i in redirected["identifiers"]}
    assert client.get("/api/mpi/links", headers=doc).json()["links"] == []

    # 7. Students may not read the MPI; every access left an audit row.
    assert client.get(f"/api/mpi/{pid}", headers=users["student"]).status_code == 403
    import audit
    actions = {row["action"] for row in audit.query(limit=200)}
    assert {"mpi.register", "mpi.read", "mpi.search", "mpi.link.resolve", "rbac.deny"} <= actions


def test_mpi_merge_runs_hooks_and_is_idempotent(client, users):
    from clinicaldb import mpi
    seen = []
    mpi.register_merge_hook(lambda conn, s, m: seen.append((s, m)))
    a = client.post("/api/mpi/register", headers=users["doctor"],
                    json={"demographics": {"family": "Ahmadi", "given": "Sara",
                                           "birth_date": "1990-01-01"}, "mrn": "A-1"}).json()
    b = client.post("/api/mpi/register", headers=users["doctor"],
                    json={"demographics": {"family": "Rostami", "given": "Nima",
                                           "birth_date": "1970-06-06"}, "mrn": "A-2"}).json()
    r = client.post("/api/mpi/merge", headers=users["doctor"],
                    json={"survivor_id": a["person_id"], "merged_id": b["person_id"],
                          "reason": "ADT^A40"})
    assert r.json()["changed"] is True
    assert (a["person_id"], b["person_id"]) in seen
    again = client.post("/api/mpi/merge", headers=users["doctor"],
                        json={"survivor_id": a["person_id"], "merged_id": b["person_id"]})
    assert again.json()["changed"] is False
    assert mpi.find_by_identifier("urn:oid:2.25.1001", "A-2") == a["person_id"]


def test_mpi_demographics_encrypted_at_rest(client, users, monkeypatch):
    import phi_crypto
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PHI_ENCRYPTION_KEYS", f"t1:{key}")
    phi_crypto.reload_keys()
    try:
        r = client.post("/api/mpi/register", headers=users["doctor"], json={
            "demographics": {"family": "Secret", "given": "Person", "birth_date": "2000-02-02",
                             "address": "12 Hidden St"}})
        pid = r.json()["person_id"]
        with db.connect() as c:
            raw = c.execute("SELECT demographics FROM mpi_persons WHERE id=?",
                            (pid,)).fetchone()["demographics"]
        assert raw.startswith("ARANMED-PHI-v1:")
        assert "Hidden" not in raw
        assert client.get(f"/api/mpi/{pid}",
                          headers=users["doctor"]).json()["demographics"]["address"] == "12 Hidden St"
    finally:
        monkeypatch.delenv("PHI_ENCRYPTION_KEYS")
        phi_crypto.reload_keys()
