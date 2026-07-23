const { createServer } = require("http");
const { parse } = require("url");
const next = require("next");
const { createProxyMiddleware } = require("http-proxy-middleware");

const dev = process.env.NODE_ENV !== "production";
const app = next({ dev });
const handle = app.getRequestHandler();

const SERVER_TIMEOUT = 300000; // 5 minutes
const HOST = process.env.HOST || "0.0.0.0";
const PORT = parseInt(process.env.PORT || "3000", 10);
const BACKEND = process.env.BACKEND_URL;
if (!BACKEND) {
  console.error(
    "BACKEND_URL is required (aranmed frontend → backend service discovery). See .env.example."
  );
  process.exit(1);
}

// Next.js rewrites do not reliably proxy POST/multipart; proxy /api in the custom server.
const apiProxy = createProxyMiddleware({
  target: BACKEND,
  changeOrigin: true,
  logLevel: "warn",
  proxyTimeout: SERVER_TIMEOUT,
  timeout: SERVER_TIMEOUT,
});

app.prepare().then(() => {
  const server = createServer((req, res) => {
    req.setTimeout(SERVER_TIMEOUT);
    res.setTimeout(SERVER_TIMEOUT);
    const parsedUrl = parse(req.url, true);

    if (parsedUrl.pathname && parsedUrl.pathname.startsWith("/api/")) {
      return apiProxy(req, res);
    }

    handle(req, res, parsedUrl);
  });

  server.timeout = SERVER_TIMEOUT;
  server.listen(PORT, HOST, (err) => {
    if (err) throw err;
    console.log(`> Ready on http://${HOST}:${PORT} (timeout: ${SERVER_TIMEOUT / 1000}s)`);
    console.log(`> API proxy -> ${BACKEND}`);
  });
});
