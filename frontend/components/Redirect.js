import { useEffect } from "react";
import { useRouter } from "next/router";

/** Keeps old bookmarks working after pages moved into the EHR section. */
export default function Redirect({ to, keepQuery = false }) {
  const router = useRouter();
  useEffect(() => {
    if (!router.isReady) return;
    const [pathname, search = ""] = to.split("?");
    const query = { ...Object.fromEntries(new URLSearchParams(search)), ...(keepQuery ? router.query : {}) };
    router.replace({ pathname, query });
  }, [router, to, keepQuery]);
  return <div className="boot">Loading…</div>;
}
