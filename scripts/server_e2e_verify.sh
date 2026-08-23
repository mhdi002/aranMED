#!/usr/bin/env bash
# =============================================================================
# End-to-end verification against a running deployment.
#
# Exercises the paths a clinician actually touches -- dictation, report
# structuring, EHR tool calling, medication alerts, RBAC, audit, FHIR, the
# rule engine -- and prints a pass/fail table. Written for a *deployed* stack
# rather than a unit-test harness: it talks only to the public gateway, so it
# verifies the same surface a browser would reach.
#
#   ./scripts/server_e2e_verify.sh [BASE_URL]
#
# BASE_URL defaults to http://localhost:8090. Credentials come from .env
# (ASR_AGENT_ADMIN_PASSWORD); the audio sample path is AUDIO_SAMPLE.
# Nothing is hardcoded to a host, port, model or password.
# =============================================================================
set -uo pipefail

BASE="${1:-${ARANMED_BASE_URL:-http://localhost:8090}}"
ENV_FILE="${ENV_FILE:-.env}"
AUDIO="${AUDIO_SAMPLE:-/root/sample.m4a}"

pass=0; fail=0; skip=0
RESULTS=()

ok()   { RESULTS+=("PASS|$1|$2"); pass=$((pass+1)); }
bad()  { RESULTS+=("FAIL|$1|$2"); fail=$((fail+1)); }
skipt(){ RESULTS+=("SKIP|$1|$2"); skip=$((skip+1)); }

jqp() { python3 -c "import sys,json;d=json.load(sys.stdin);$1" 2>/dev/null; }

echo "=== AranMed end-to-end verification ==="
echo "    target: $BASE"
echo ""

# --- auth -------------------------------------------------------------------
ADMIN_PW=$(grep '^ASR_AGENT_ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)
ADMIN_USER=$(grep '^ASR_AGENT_ADMIN_USER=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-)
ADMIN_USER="${ADMIN_USER:-admin}"

TOK=$(curl -s -X POST "$BASE/api/auth/login" \
      -d "username=${ADMIN_USER}&password=${ADMIN_PW}" | jqp "print(d.get('access_token',''))")
if [ -n "$TOK" ]; then ok "auth.login" "token issued for $ADMIN_USER"
else bad "auth.login" "no token — every authenticated check below will fail"; fi
AUTH=(-H "Authorization: Bearer $TOK")

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# --- infrastructure ---------------------------------------------------------
H=$(curl -s "$BASE/api/health")
[ -n "$H" ] && ok "health" "$(echo "$H" | jqp "print('asr='+str(d['asr_model'])+' core='+str(d['core_model']))")" \
            || bad "health" "no response"

CH=$(echo "$H" | jqp "print(d.get('medrag',{}).get('chunks',0))")
[ "${CH:-0}" -gt 0 ] && ok "medrag.corpus" "$CH chunks" || skipt "medrag.corpus" "0 chunks (corpus not attached)"

T=$(curl -s "$BASE/api/templates" | jqp "print(len(d['templates']))")
[ "${T:-0}" -gt 0 ] && ok "templates" "$T loaded" || bad "templates" "none"

# --- template auto-selection ------------------------------------------------
S=$(curl -s -X POST "$BASE/api/templates/suggest" -H "Content-Type: application/json" \
    -d '{"transcript":"hepato biliary sonography, liver normal size, gallbladder normal","top_k":1}' \
    | jqp "s=d['suggestions'][0];print(s['template_id'],round(s['confidence'],2))")
[ -n "$S" ] && ok "template.autoselect" "$S" || bad "template.autoselect" "no suggestion"

# --- ASR --------------------------------------------------------------------
if [ -f "$AUDIO" ]; then
  A=$(curl -s -X POST "$BASE/api/transcribe" -F "file=@$AUDIO")
  AM=$(echo "$A" | jqp "print(d.get('asr_model',''))")
  AT=$(echo "$A" | jqp "print(len(d.get('text','')))")
  [ "${AT:-0}" -gt 20 ] && ok "asr.transcribe" "$AM, ${AT} chars" || bad "asr.transcribe" "empty/short"
  echo "$A" | jqp "print(d.get('text',''))" > /tmp/transcript.txt
else
  skipt "asr.transcribe" "no audio at $AUDIO"
fi

# --- full dictate -----------------------------------------------------------
if [ -f "$AUDIO" ]; then
  D=$(curl -s -X POST "$BASE/api/dictate" -F "file=@$AUDIO")
  echo "$D" > /tmp/dictate.json
  RC=$(echo "$D" | jqp "print(len(d.get('report','')))")
  [ "${RC:-0}" -gt 100 ] \
    && ok "dictate.e2e" "$(echo "$D" | jqp "print('tpl='+str(d['template_id'])+' model='+str(d['model'])+' chars='+str(len(d['report'])))")" \
    || bad "dictate.e2e" "$(echo "$D" | head -c 120)"
else
  skipt "dictate.e2e" "no audio"
fi

# --- report from text (no audio) --------------------------------------------
R=$(curl -s -X POST "$BASE/api/report" -H "Content-Type: application/json" \
    -d '{"transcript":"Thyroid sonography. Both lobes normal in size and echogenicity. No nodule."}')
RL=$(echo "$R" | jqp "print(len(d.get('report','')))")
[ "${RL:-0}" -gt 50 ] && ok "report.from_text" "$(echo "$R" | jqp "print('tpl='+str(d['template_id'])+' chars='+str(len(d['report'])))")" \
                      || bad "report.from_text" "$(echo "$R" | head -c 120)"

# --- rule engine ------------------------------------------------------------
RU=$(curl -s "${AUTH[@]}" "$BASE/api/rules")
RN=$(echo "$RU" | jqp "
r=d.get('rules') if isinstance(d,dict) else d
print(len(r) if r is not None else 0)")
[ "${RN:-0}" -gt 0 ] && ok "rules.engine" "$RN rules" || skipt "rules.engine" "0 rules exposed"

# --- EHR CRUD + tool-calling surface ---------------------------------------
EB=$(curl -s -X POST "$BASE/api/ehr/build" "${AUTH[@]}" -H "Content-Type: application/json" \
     -d '{"patient_info":"Sara Ahmadi, MRN E2E-001, 54 year old female. Hypertension. Allergic to penicillin causing rash. Taking amlodipine 5mg once daily and metformin 500mg every 12 hours for diabetes.","language":"en"}')
PID=$(echo "$EB" | jqp "print(d.get('patient_id',''))")
if [ -n "$PID" ]; then
  ok "ehr.build" "patient_id=$PID"
  echo "$EB" > /tmp/ehr.json

  G=$(code "${AUTH[@]}" "$BASE/api/ehr/$PID")
  [ "$G" = "200" ] && ok "ehr.read" "200" || bad "ehr.read" "$G"

  L=$(curl -s "${AUTH[@]}" "$BASE/api/ehr" | jqp "print(len(d['records']))")
  [ "${L:-0}" -gt 0 ] && ok "ehr.list" "$L record(s)" || bad "ehr.list" "empty"

  # PHI encryption: the stored blob must not contain the patient's name.
  ENC=$(docker compose exec -T backend python -c "
import sys;sys.path.insert(0,'/app/backend')
import db,phi_crypto
with db.connect() as c:
    r=c.execute('SELECT name,data FROM patients WHERE id=?',('$PID',)).fetchone()
print('enc' if r and phi_crypto.is_encrypted(r['data']) else 'plain',
      'leak' if r and ('Sara' in (r['data'] or '') or 'Sara' in (r['name'] or '')) else 'noleak')
" 2>/dev/null)
  case "$ENC" in
    "enc noleak") ok "phi.encryption" "ciphertext at rest, no cleartext name" ;;
    "") skipt "phi.encryption" "could not inspect the database" ;;
    *) bad "phi.encryption" "$ENC" ;;
  esac

  # Medication alerts derived from the dosing schedule.
  AL=$(curl -s -X POST "$BASE/api/alerts/check" "${AUTH[@]}" -H "Content-Type: application/json" \
       -d "{\"patient_id\":\"$PID\"}")
  AN=$(echo "$AL" | jqp "print(len(d.get('medications',[])))")
  [ "${AN:-0}" -gt 0 ] && ok "alerts.check" "$AN medication(s) tracked" || bad "alerts.check" "$(echo "$AL" | head -c 100)"

  # Dose recording resets the schedule.
  DR=$(code -X POST "$BASE/api/ehr/$PID/dose" "${AUTH[@]}" -H "Content-Type: application/json" \
       -d '{"medication":"amlodipine"}')
  [ "$DR" = "200" ] && ok "ehr.record_dose" "200" || bad "ehr.record_dose" "$DR"

  # FHIR projection.
  FB=$(curl -s "${AUTH[@]}" "$BASE/api/fhir/Patient/$PID/\$everything")
  FT=$(echo "$FB" | jqp "
from collections import Counter
c=Counter(e['resource']['resourceType'] for e in d.get('entry',[]))
print(str(d.get('total',0))+' '+','.join(f'{k}={v}' for k,v in c.items()))")
  [ -n "$FT" ] && ok "fhir.everything" "$FT" || bad "fhir.everything" "no bundle"

  # Terminology binding: did any concept get a real code?
  CODED=$(echo "$FB" | jqp "
n=0
for e in d.get('entry',[]):
    r=e['resource']
    for f in ('code','medicationCodeableConcept'):
        v=r.get(f)
        if isinstance(v,dict) and v.get('coding'): n+=1
print(n)")
  [ "${CODED:-0}" -gt 0 ] && ok "terminology.binding" "$CODED concept(s) coded" || skipt "terminology.binding" "all text-only"
else
  bad "ehr.build" "$(echo "$EB" | head -c 160)"
  for t in ehr.read ehr.list phi.encryption alerts.check ehr.record_dose fhir.everything terminology.binding; do
    skipt "$t" "depends on ehr.build"
  done
fi

# --- governance -------------------------------------------------------------
AU=$(curl -s "${AUTH[@]}" "$BASE/api/audit?limit=5" | jqp "print(d.get('total',0))")
[ "${AU:-0}" -gt 0 ] && ok "audit.trail" "$AU entries" || bad "audit.trail" "empty"

SE=$(curl -s "${AUTH[@]}" "$BASE/api/auth/sessions" | jqp "print(d.get('count',0))")
[ "${SE:-0}" -ge 1 ] && ok "auth.sessions" "$SE open" || bad "auth.sessions" "none"

MO=$(code "${AUTH[@]}" "$BASE/api/models")
[ "$MO" = "200" ] && ok "models.registry" "200" || bad "models.registry" "$MO"

# --- RBAC: a student must be refused EHR ------------------------------------
SPW="Rbac-Probe-$(date +%s)-Xyz"
SU="rbac_probe_$(date +%s)"
curl -s -o /dev/null -X POST "$BASE/api/auth/register" -H "Content-Type: application/json" \
  -d "{\"username\":\"$SU\",\"password\":\"$SPW\",\"role\":\"student\"}"
STOK=$(curl -s -X POST "$BASE/api/auth/login" -d "username=$SU&password=$SPW" | jqp "print(d.get('access_token',''))")
if [ -n "$STOK" ]; then
  E1=$(code -H "Authorization: Bearer $STOK" "$BASE/api/ehr")
  E2=$(code -H "Authorization: Bearer $STOK" "$BASE/api/audit")
  E3=$(code -H "Authorization: Bearer $STOK" "$BASE/api/education/saved")
  [ "$E1" = "403" ] && [ "$E2" = "403" ] && [ "$E3" = "200" ] \
    && ok "rbac.enforcement" "student: ehr=403 audit=403 education=200" \
    || bad "rbac.enforcement" "ehr=$E1 audit=$E2 education=$E3"
else
  skipt "rbac.enforcement" "could not create probe user"
fi

# --- knowledge (needs the corpus) -------------------------------------------
if [ "${CH:-0}" -gt 0 ]; then
  K=$(curl -s -X POST "$BASE/api/knowledge/ask" "${AUTH[@]}" -H "Content-Type: application/json" \
      -d '{"question":"What are the sonographic features of fatty liver?"}')
  KL=$(echo "$K" | jqp "print(len(str(d.get('answer','')))) ")
  [ "${KL:-0}" -gt 40 ] && ok "knowledge.ask" "${KL} chars" || bad "knowledge.ask" "empty"
else
  skipt "knowledge.ask" "no corpus attached"
fi

# --- interoperability -------------------------------------------------------
HL=$(curl -s -X POST "$BASE/api/hl7/oru" "${AUTH[@]}" -H "Content-Type: application/json" \
     -d '{"patient_id":"E2E-001","report_text":"IMPRESSION: Normal study.","accession":"ACC-E2E","final":true}' \
     | jqp "print(len(d.get('message','')))")
[ "${HL:-0}" -gt 50 ] && ok "hl7.oru" "${HL} chars" || bad "hl7.oru" "failed"

# --- cleanup ----------------------------------------------------------------
[ -n "${PID:-}" ] && curl -s -o /dev/null -X DELETE "${AUTH[@]}" "$BASE/api/ehr/$PID"

# --- report -----------------------------------------------------------------
echo ""
printf "%-6s %-22s %s\n" "STATUS" "CHECK" "DETAIL"
printf "%-6s %-22s %s\n" "------" "----------------------" "------"
for r in "${RESULTS[@]}"; do
  IFS='|' read -r st name detail <<< "$r"
  printf "%-6s %-22s %s\n" "$st" "$name" "$detail"
done
echo ""
echo "passed=$pass failed=$fail skipped=$skip"
[ "$fail" -eq 0 ]
