import { useEffect } from "react";
import { useRouter } from "next/router";
import { useAuth } from "../lib/auth";
import { defaultViewForRole } from "../lib/roles";
import { routeForView } from "../lib/viewMeta";

/**
 * `/` is a redirect to the signed-in user's default view — every workspace
 * now has a real, deep-linkable route (see components/AppShell.js and
 * pages/{dictate,radiology,reports,templates,ehr,alerts,education,asr,llm,
 * vision,settings}.js). This replaced the old single-route `view` state
 * switcher.
 */
export default function IndexRedirect() {
  const { user, ready } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!ready) return;
    if (!user) { router.replace("/login"); return; }
    router.replace(routeForView(defaultViewForRole(user.role || "doctor")));
  }, [ready, user, router]);

  return <div className="boot">Loading…</div>;
}
