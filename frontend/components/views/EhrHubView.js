import { useRouter } from "next/router";
import { useAuth } from "../../lib/auth";
import { useT } from "../../lib/i18n";
import { ehrTabsForRole } from "../../lib/roles";
import ClinicalSearchView from "./ClinicalSearchView";
import TransfersView from "./TransfersView";
import EmsView from "./EmsView";
import AlertsView from "./AlertsView";
import EhrView from "./EhrView";

// Everything about a patient lives here: one EHR section, one tab per area.
const TABS = {
  patients: ClinicalSearchView,
  transfers: TransfersView,
  ems: EmsView,
  alerts: AlertsView,
  intake: EhrView,
};

export default function EhrHubView() {
  const { t } = useT();
  const { user } = useAuth();
  const router = useRouter();
  const allowed = ehrTabsForRole(user?.role || "doctor");
  const requested = String(router.query.tab || "");
  const tab = allowed.includes(requested) ? requested : allowed[0];
  const View = TABS[tab];

  const pick = (id) => router.replace({ pathname: "/ehr", query: { tab: id } }, undefined, { shallow: true });

  return (
    <div data-testid="ehr-hub">
      <div className="chart-tabs ehr-tabs" role="tablist" aria-label={t("nav.ehr")}>
        {allowed.map((id) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id}
                  className={`tab ${tab === id ? "active" : ""}`} onClick={() => pick(id)} data-ehr-tab={id}>
            {t(`ehr.tab.${id}`)}
          </button>
        ))}
      </div>
      {View ? <View key={tab} /> : null}
    </div>
  );
}
