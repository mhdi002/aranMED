import "../styles/tokens.css";
import "../styles/globals.css";
import "../styles/clinical.css";
import { useRouter } from "next/router";
import { useEffect } from "react";
import { AuthProvider, useAuth } from "../lib/auth";
import { LanguageProvider } from "../lib/i18n";

function Gate({ Component, pageProps }) {
  const { user, ready } = useAuth();
  const router = useRouter();
  const isPublic = router.pathname === "/login";

  useEffect(() => {
    if (!ready) return;
    if (!user && !isPublic) router.replace("/login");
  }, [ready, user, isPublic, router]);

  if (!ready) {
    return (
      <div className="boot">
        Loading…
      </div>
    );
  }
  if (!user && !isPublic) return null;
  return <Component {...pageProps} />;
}

export default function App({ Component, pageProps }) {
  return (
    <LanguageProvider>
      <AuthProvider>
        <Gate Component={Component} pageProps={pageProps} />
      </AuthProvider>
    </LanguageProvider>
  );
}
