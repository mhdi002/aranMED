import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../lib/auth";
import { apiFetch } from "../../lib/api";
import { useT } from "../../lib/i18n";
import { Badge, Button, Card, CardHead, ConfirmModal, Label, Modal, Select, TextInput, useToast } from "../ui";

const ROLES = ["doctor", "radiologist", "resident", "student", "admin"];
const BLANK = { username: "", password: "", email: "", role: "doctor" };

/** Admin-only account management. Privileged roles cannot self-register
 *  (SELF_REGISTER_ROLES), so this is where doctors and radiologists are made,
 *  and where accounts are disabled, re-roled or given a new password. */
export default function UsersAdmin() {
  const { token, user: me } = useAuth();
  const { t, lang } = useT();
  const toast = useToast();
  const [users, setUsers] = useState([]);
  const [selfRoles, setSelfRoles] = useState([]);
  const [form, setForm] = useState(BLANK);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null); // {kind: "disable"|"enable"|"reset", user}
  const [acting, setActing] = useState(false);
  const [temp, setTemp] = useState(null); // {username, password} after a reset

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const r = await apiFetch("/api/auth/users", {}, token);
      setUsers(r.users); setSelfRoles(r.self_register_roles);
    } catch (e) { setErr(e.message); }
  }, [token]);
  useEffect(() => { load(); }, [load]);

  async function create(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const r = await apiFetch("/api/auth/register", {
        method: "POST", body: JSON.stringify({ ...form, email: form.email || null }),
      }, token);
      toast.success(t("users.created", { name: r.user.username, role: t(`auth.role.${r.user.role}`) }));
      setForm(BLANK); load();
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  }

  async function changeRole(u, role) {
    setErr("");
    try {
      await apiFetch(`/api/auth/users/${u.id}`, { method: "PATCH", body: JSON.stringify({ role }) }, token);
      toast.success(t("users.roleChanged", { name: u.username }));
      load();
    } catch (e) { setErr(e.message); load(); }
  }

  async function confirmAction() {
    const { kind, user: u } = pending;
    setActing(true); setErr("");
    try {
      if (kind === "reset") {
        const r = await apiFetch(`/api/auth/users/${u.id}/reset-password`, { method: "POST" }, token);
        setTemp({ username: r.username, password: r.temporary_password });
      } else {
        await apiFetch(`/api/auth/users/${u.id}`, {
          method: "PATCH", body: JSON.stringify({ disabled: kind === "disable" }),
        }, token);
        toast.success(t(kind === "disable" ? "users.disabledMsg" : "users.enabledMsg", { name: u.username }));
      }
      load();
    } catch (e) { setErr(e.message); } finally { setActing(false); setPending(null); }
  }

  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <Card data-testid="users-admin">
      <CardHead step="👤" title={t("users.title")} hint={t("users.hint")} />
      <p className="muted" style={{ lineHeight: 1.6 }}>
        {t("users.selfRoles")}{" "}
        <b>{selfRoles.length ? selfRoles.map((r) => t(`auth.role.${r}`)).join(", ") : t("users.selfNone")}</b>{" "}
        (<code>SELF_REGISTER_ROLES</code>). {t("users.createHere")}
      </p>
      <form className="filter-grid" onSubmit={create}>
        <div><Label>{t("auth.username")}</Label><TextInput value={form.username} onChange={f("username")} required minLength={3} autoComplete="off" /></div>
        <div><Label>{t("users.initialPw")}</Label><TextInput type="password" value={form.password} onChange={f("password")} required autoComplete="new-password" /></div>
        <div><Label>{t("auth.email")}</Label><TextInput type="email" value={form.email} onChange={f("email")} /></div>
        <div><Label>{t("users.role")}</Label>
          <Select value={form.role} onChange={f("role")}>
            {ROLES.map((r) => <option key={r} value={r}>{t(`auth.role.${r}`)}</option>)}
          </Select></div>
        <div className="filter-actions"><Button type="submit" loading={busy} data-testid="user-create">{t("users.create")}</Button></div>
      </form>
      {err && <div className="err" role="alert">{err}</div>}
      <div className="table-scroll" style={{ marginTop: 16 }}>
        <table className="data-table" data-testid="users-table">
          <thead><tr>
            <th>{t("auth.username")}</th><th>{t("users.role")}</th><th>{t("auth.email")}</th>
            <th>MFA</th><th>{t("users.status")}</th><th>{t("users.createdAt")}</th>
            <th><span className="sr-only">{t("ch.col.actions")}</span></th>
          </tr></thead>
          <tbody>
            {users.map((u) => {
              const self = me && u.id === me.id;
              return (
                <tr key={u.id}>
                  <td className="bold">{u.username}</td>
                  <td>
                    <Select value={u.role} disabled={self} aria-label={t("users.roleFor", { name: u.username })}
                            onChange={(e) => changeRole(u, e.target.value)}>
                      {ROLES.map((r) => <option key={r} value={r}>{t(`auth.role.${r}`)}</option>)}
                    </Select>
                  </td>
                  <td>{u.email || "—"}</td>
                  <td>{u.mfa_enabled ? <Badge tone="ok">{t("users.on")}</Badge> : "—"}</td>
                  <td>{u.disabled ? <Badge tone="err">{t("users.disabled")}</Badge> : <Badge tone="ok">{t("users.active")}</Badge>}</td>
                  <td className="small">{u.created_at ? new Date(u.created_at * 1000).toLocaleDateString(lang === "fa" ? "fa-IR" : undefined) : "—"}</td>
                  <td className="nowrap">
                    <Button variant="ghost" onClick={() => setPending({ kind: "reset", user: u })}>{t("users.resetPw")}</Button>
                    {!self && (u.disabled
                      ? <Button variant="ghost" onClick={() => setPending({ kind: "enable", user: u })}>{t("users.enable")}</Button>
                      : <Button variant="danger" onClick={() => setPending({ kind: "disable", user: u })}>{t("users.disable")}</Button>)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <ConfirmModal open={!!pending} danger={pending?.kind !== "enable"} busy={acting}
                    title={pending ? t(`users.confirm.${pending.kind}`, { name: pending.user.username }) : ""}
                    confirmLabel={pending ? t(`users.${pending.kind === "reset" ? "resetPw" : pending.kind}`) : ""}
                    onCancel={() => setPending(null)} onConfirm={confirmAction}>
        {pending && t(`users.confirmText.${pending.kind}`)}
      </ConfirmModal>

      <Modal open={!!temp} title={t("users.tempTitle")} onClose={() => setTemp(null)}
             actions={<button className="btn" onClick={() => setTemp(null)}>{t("common.close")}</button>}>
        {temp && <>
          <p>{t("users.tempText", { name: temp.username })}</p>
          <pre className="raw" data-testid="temp-password">{temp.password}</pre>
        </>}
      </Modal>
    </Card>
  );
}
