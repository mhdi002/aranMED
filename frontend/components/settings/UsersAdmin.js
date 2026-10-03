import { useCallback, useEffect, useState } from "react";
import { apiFetch, useAuth } from "../../lib/auth";
import { apiUrl } from "../../lib/config";
import { Badge, Button, Card, CardHead, Label, Select, TextInput } from "../ui";

const ROLES = ["doctor", "radiologist", "resident", "student", "admin"];
const BLANK = { username: "", password: "", email: "", role: "doctor" };

/** Admin-only account management. Privileged roles cannot self-register
 *  (SELF_REGISTER_ROLES), so this is where doctors and radiologists are made. */
export default function UsersAdmin() {
  const { token } = useAuth();
  const [users, setUsers] = useState([]);
  const [selfRoles, setSelfRoles] = useState([]);
  const [form, setForm] = useState(BLANK);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const r = await apiFetch(apiUrl("/api/auth/users"), {}, token);
      setUsers(r.users); setSelfRoles(r.self_register_roles);
    } catch (e) { setErr(e.message); }
  }, [token]);
  useEffect(() => { load(); }, [load]);

  async function create(e) {
    e.preventDefault();
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await apiFetch(apiUrl("/api/auth/register"), {
        method: "POST", body: JSON.stringify({ ...form, email: form.email || null }),
      }, token);
      setMsg(`Created ${r.user.username} (${r.user.role}).`);
      setForm(BLANK); load();
    } catch (e2) { setErr(e2.message); } finally { setBusy(false); }
  }
  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <Card data-testid="users-admin">
      <CardHead step="👤" title="Users" hint="administrator only" />
      <p className="muted" style={{ lineHeight: 1.6 }}>
        Visitors can register themselves only as{" "}
        <b>{selfRoles.length ? selfRoles.join(", ") : "nobody (self-registration disabled)"}</b>{" "}
        (<code>SELF_REGISTER_ROLES</code>). Create clinical and admin accounts here.
      </p>
      <form className="filter-grid" onSubmit={create}>
        <div><Label>Username</Label><TextInput value={form.username} onChange={f("username")} required minLength={3} /></div>
        <div><Label>Initial password</Label><TextInput type="password" value={form.password} onChange={f("password")} required /></div>
        <div><Label>Email</Label><TextInput type="email" value={form.email} onChange={f("email")} /></div>
        <div><Label>Role</Label>
          <Select value={form.role} onChange={f("role")}>
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </Select></div>
        <div className="filter-actions"><Button type="submit" loading={busy} data-testid="user-create">Create user</Button></div>
      </form>
      {msg && <p className="muted">{msg}</p>}
      {err && <div className="err">{err}</div>}
      <div className="table-scroll" style={{ marginTop: 16 }}>
        <table className="data-table" data-testid="users-table">
          <thead><tr><th>Username</th><th>Role</th><th>Email</th><th>MFA</th><th>Created</th></tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td className="bold">{u.username}</td>
                <td><Badge tone={u.role === "admin" ? "warn" : "info"}>{u.role}</Badge></td>
                <td>{u.email || "—"}</td>
                <td>{u.mfa_enabled ? <Badge tone="ok">on</Badge> : "—"}</td>
                <td className="small">{u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
