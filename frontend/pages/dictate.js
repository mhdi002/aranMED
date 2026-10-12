import AppShell from "../components/AppShell";
import DictateView from "../components/views/DictateView";
import { useAuth } from "../lib/auth";

export default function DictatePage() {
  const { user } = useAuth();
  return (
    <AppShell>
      <DictateView userRole={user?.role || "doctor"} />
    </AppShell>
  );
}
