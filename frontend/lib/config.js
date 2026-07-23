/** API base URLs — browser uses same-origin /api (works through Cloudflare tunnel).
 *  Server-side / custom proxy reads BACKEND_URL from the environment (see .env.example).
 *  No deploy host is hardcoded for browser calls.
 */
export const SERVER_BACKEND = (process.env.BACKEND_URL || "").replace(/\/$/, "");

/** Build an API URL for browser or SSR. */
export function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  // Browser: always same-origin relative /api (proxied by frontend server).
  if (typeof window !== "undefined") {
    const override = process.env.NEXT_PUBLIC_BACKEND_URL;
    if (override) {
      return `${override.replace(/\/$/, "")}${p}`;
    }
    return p;
  }
  // SSR / Node: require BACKEND_URL (microservice discovery).
  if (!SERVER_BACKEND) {
    throw new Error(
      "BACKEND_URL must be set for server-side API calls (see .env.example)"
    );
  }
  return `${SERVER_BACKEND}${p}`;
}

/** @deprecated use apiUrl() */
export const BACKEND_URL = SERVER_BACKEND;
