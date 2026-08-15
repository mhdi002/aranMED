import AppShell from "../components/AppShell";
import ModelsView from "../components/views/ModelsView";

export default function LlmPage() {
  return (
    <AppShell>
      <ModelsView kind="llm" />
    </AppShell>
  );
}
