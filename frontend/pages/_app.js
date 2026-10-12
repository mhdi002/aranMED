import "../styles/tokens.css";
import "../styles/globals.css";
import "../styles/clinical.css";
import { useRouter } from "next/router";
import { useEffect } from "react";
import { AuthProvider, useAuth } from "../lib/auth";
import { LanguageProvider, useT } from "../lib/i18n";
import { ThemeProvider } from "../lib/theme";
import { ToastProvider } from "../components/ui";

function Gate({ Component, pageProps }) {
  const { user, ready } = useAuth();
  const { t } = useT();
  const router = useRouter();
  const isPublic = router.pathname === "/login";

  useEffect(() => {
    if (!ready) return;
    if (!user && !isPublic) router.replace("/login");
  }, [ready, user, isPublic, router]);

  if (!ready) {
    return <div className="boot" role="status">{t("common.loading")}</div>;
  }
  if (!user && !isPublic) return null;
  return <Component {...pageProps} />;
}

export default function App({ Component, pageProps }) {
  return (
    <LanguageProvider>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider>
            <Gate Component={Component} pageProps={pageProps} />
          </AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </LanguageProvider>
  );
}
