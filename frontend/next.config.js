const crypto = require("crypto");
const prepaint = require("./lib/prepaint.json");

const dev = process.env.NODE_ENV !== "production";
const prepaintHash = crypto.createHash("sha256").update(prepaint.script).digest("base64");

// Content Security Policy for the app pages. The bearer token lives in
// localStorage, so the policy's job is to make injected script unable to
// run: only our own bundles and the hashed pre-paint snippet execute.
//   - 'wasm-unsafe-eval' + blob: workers: Cornerstone's image decoders.
//   - blob:/data: images and media: thumbnails, rendered frames, PDF/video.
//   - style 'unsafe-inline': React style attributes.
//   - dev only: 'unsafe-eval' for Next's fast refresh.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'sha256-${prepaintHash}' 'wasm-unsafe-eval'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "media-src 'self' blob:",
  "font-src 'self' data:",
  `connect-src 'self'${process.env.NEXT_PUBLIC_BACKEND_URL ? ` ${process.env.NEXT_PUBLIC_BACKEND_URL}` : ""}${dev ? " ws:" : ""}`,
  "worker-src 'self' blob:",
  "frame-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Dictation needs the microphone; nothing else is used.
  { key: "Permissions-Policy", value: "microphone=(self), camera=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://127.0.0.1:8010";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

module.exports = nextConfig;
