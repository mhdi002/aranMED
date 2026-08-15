import AppShell from "../components/AppShell";
import ModelsView from "../components/views/ModelsView";

export default function VisionPage() {
  return (
    <AppShell>
      <ModelsView kind="vision" />
    </AppShell>
  );
}
