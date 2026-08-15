import AppShell from "../components/AppShell";
import DictateView from "../components/views/DictateView";
import { useAuth } from "../lib/auth";
import { SERVER_BACKEND } from "../lib/config";

export async function getServerSideProps() {
  const base = SERVER_BACKEND;
  let templates = [], health = null;
  try {
    const [tr, hr] = await Promise.all([
      fetch(`${base}/api/templates`).then((r) => r.json()),
      fetch(`${base}/api/health`).then((r) => r.json()),
    ]);
    templates = tr.templates || [];
    health = hr;
  } catch (_) {}
  return { props: { initialTemplates: templates, initialHealth: health } };
}

export default function DictatePage({ initialTemplates, initialHealth }) {
  const { user } = useAuth();
  return (
    <AppShell>
      <DictateView
        initialTemplates={initialTemplates}
        initialHealth={initialHealth}
        userRole={user?.role || "doctor"}
      />
    </AppShell>
  );
}
