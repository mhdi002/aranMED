import AppShell from "../components/AppShell";
import ModelsView from "../components/views/ModelsView";

export default function AsrPage() {
  return (
    <AppShell>
      <ModelsView kind="asr" />
    </AppShell>
  );
}
