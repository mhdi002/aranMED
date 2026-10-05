import { useState } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { clinicalApi, ageFrom } from "../../lib/clinical";
import { Badge, Button, Card, CardHead, EmptyState, Label, Modal, Select, TextInput } from "../ui";

const BLANK = { family: "", given: "", birth_date: "", sex: "", phone: "", mrn: "", national_id: "" };

export default function ClinicalSearchView() {
  const { t } = useT();
  const { token } = useAuth();
  const router = useRouter();
  const [q, setQ] = useState("");
  const [birth, setBirth] = useState("");
  const [network, setNetwork] = useState(false);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(BLANK);

  async function search(e) {
    e?.preventDefault();
    if (!q && !birth) return;
    setBusy(true); setErr("");
    try {
      setRes(await clinicalApi.search({ q, birth_date: birth, scope: network ? "network" : "local" }, token));
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  }

  async function register(e) {
    e.preventDefault();
    try {
      const { mrn, national_id, ...demo } = form;
      const r = await clinicalApi.register({ demographics: demo, mrn: mrn || null, national_id: national_id || null }, token);
      setOpen(false);
      router.push(`/ehr/chart?id=${r.person_id}`);
    } catch (e2) { setErr(e2.message); }
  }
  const f = (k) => (e) => setForm((x) => ({ ...x, [k]: e.target.value }));

  return (
    <div>
      <div className="page-head">
        <h2 className="page-title">{t("cl.title")}</h2>
        <p className="page-sub">{t("cl.sub")}</p>
      </div>
      <Card>
        <form className="filter-grid" onSubmit={search}>
          <div style={{ gridColumn: "span 2" }}><Label>{t("cl.query")}</Label>
            <TextInput value={q} onChange={(e) => setQ(e.target.value)} dir="auto" name="q" autoFocus /></div>
          <div><Label>{t("cl.birth")}</Label><TextInput type="date" value={birth} onChange={(e) => setBirth(e.target.value)} /></div>
          <div className="checks" style={{ alignSelf: "center" }}>
            <label><input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} name="network" /> {t("cl.network")}</label>
          </div>
          <div className="filter-actions">
            <Button type="submit" loading={busy}>{t("cl.search")}</Button>
            <Button type="button" variant="ghost" onClick={() => setOpen(true)} data-testid="register-open">{t("cl.new")}</Button>
          </div>
        </form>
      </Card>
      {err && <div className="err">{err}</div>}
      {res && (
        <Card data-testid="patient-results">
          <CardHead title={t("cl.results")} right={<span className="muted">{res.results.length + res.remote.length}</span>} />
          {res.results.length === 0 && res.remote.length === 0 && <EmptyState>{t("cl.none")}</EmptyState>}
          <ul className="mini-list">
            {res.results.map((p) => (
              <li key={p.person_id} className="list-row">
                <div>
                  <div className="bold" dir="auto">{p.name} {p.restricted && <Badge tone="err">{t("cl.restricted")}</Badge>}</div>
                  <div className="muted small">
                    {p.demographics.sex || "?"} · {p.demographics.birth_date || "?"}
                    {ageFrom(p.demographics.birth_date) ? ` (${ageFrom(p.demographics.birth_date)}y)` : ""} ·{" "}
                    {p.identifiers.slice(0, 3).map((i) => i.value).join(" · ")}
                  </div>
                </div>
                <Button variant="ghost" onClick={() => router.push(`/ehr/chart?id=${p.person_id}`)}>{t("cl.open")}</Button>
              </li>
            ))}
          </ul>
          {res.remote.length > 0 && (
            <>
              <div className="lbl">{t("cl.remote")}</div>
              <ul className="mini-list">
                {res.remote.map((p, i) => (
                  <li key={i}><span className="src-badge remote">{p.facility}</span>{" "}
                    <b dir="auto">{p.name}</b> <span className="muted small">{p.demographics.birth_date || ""} · {p.identifiers.map((x) => x.value).join(" · ")}</span></li>
                ))}
              </ul>
            </>
          )}
          {res.errors?.length > 0 && <div className="warn-banner">{t("cl.unreachable")}: {res.errors.map((e) => e.facility).join(", ")}</div>}
        </Card>
      )}
      <Modal open={open} title={t("cl.new")} onClose={() => setOpen(false)}>
        <form className="form-grid" onSubmit={register}>
          <div><Label>{t("cl.family")}</Label><TextInput value={form.family} onChange={f("family")} dir="auto" required /></div>
          <div><Label>{t("cl.given")}</Label><TextInput value={form.given} onChange={f("given")} dir="auto" required /></div>
          <div><Label>{t("cl.birth")}</Label><TextInput type="date" value={form.birth_date} onChange={f("birth_date")} required /></div>
          <div><Label>{t("cl.sex")}</Label><Select value={form.sex} onChange={f("sex")}><option value="" /><option value="female">female</option><option value="male">male</option><option value="other">other</option></Select></div>
          <div><Label>{t("cl.national")}</Label><TextInput value={form.national_id} onChange={f("national_id")} /></div>
          <div><Label>{t("cl.mrn")}</Label><TextInput value={form.mrn} onChange={f("mrn")} /></div>
          <div className="span-2"><Label>{t("cl.phone")}</Label><TextInput value={form.phone} onChange={f("phone")} /></div>
          <div className="span-2 toolbar"><Button type="submit">{t("cl.registerBtn")}</Button></div>
        </form>
      </Modal>
    </div>
  );
}
