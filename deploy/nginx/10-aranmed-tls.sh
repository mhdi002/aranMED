#!/bin/sh
# =============================================================================
# Stage the AranMed gateway config, optionally switching it to TLS.
#
# Runs from nginx's /docker-entrypoint.d/ before 20-envsubst-on-templates.sh,
# so everything it writes still gets the normal ${VAR} substitution pass.
#
# There is deliberately ONE template (nginx.conf.template). Rather than keep a
# near-identical TLS copy that drifts from it, this script performs a surgical
# transformation when GATEWAY_TLS_ENABLED is truthy:
#   * swap the plain `listen` for a TLS listener (+ HTTP/2, HSTS)
#   * prepend a small server block that redirects plain HTTP to HTTPS
#
# Every value is an environment variable; nothing is baked in. With TLS off
# the template is copied through byte-for-byte, so the default deployment
# behaves exactly as it did before this script existed.
# See docs/core/DEPLOYMENT.md.
# =============================================================================
set -eu

SRC="${GATEWAY_TEMPLATE_SRC:-/etc/nginx/aranmed/nginx.conf.template}"
DEST="${GATEWAY_TEMPLATE_DEST:-/etc/nginx/templates/default.conf.template}"

if [ ! -f "$SRC" ]; then
    echo "10-aranmed-tls.sh: template not found at $SRC" >&2
    exit 1
fi

mkdir -p "$(dirname "$DEST")"

case "$(printf '%s' "${GATEWAY_TLS_ENABLED:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) tls_on=1 ;;
    *)             tls_on=0 ;;
esac

if [ "$tls_on" -eq 0 ]; then
    cp "$SRC" "$DEST"
    echo "10-aranmed-tls.sh: TLS disabled — serving plain HTTP on \${GATEWAY_PORT}"
    exit 0
fi

cert="${GATEWAY_TLS_CERT:-/etc/nginx/certs/tls.crt}"
key="${GATEWAY_TLS_KEY:-/etc/nginx/certs/tls.key}"

# Fail loudly at start rather than serving plaintext when TLS was asked for.
if [ ! -r "$cert" ] || [ ! -r "$key" ]; then
    echo "10-aranmed-tls.sh: GATEWAY_TLS_ENABLED is set but the certificate or key" >&2
    echo "  is missing/unreadable (cert=$cert key=$key). Mount them into the" >&2
    echo "  gateway container, or unset GATEWAY_TLS_ENABLED." >&2
    exit 1
fi

# 1) Turn the existing listener into a TLS listener.
#    `listen ${GATEWAY_PORT};` is the single line the base template uses.
awk '
    /^[[:space:]]*listen[[:space:]]+\$\{GATEWAY_PORT\};[[:space:]]*$/ {
        print "    listen ${GATEWAY_TLS_PORT} ssl;"
        print "    http2 on;"
        print ""
        print "    ssl_certificate     ${GATEWAY_TLS_CERT};"
        print "    ssl_certificate_key ${GATEWAY_TLS_KEY};"
        print "    ssl_protocols       ${GATEWAY_TLS_PROTOCOLS};"
        print "    ssl_ciphers         ${GATEWAY_TLS_CIPHERS};"
        print "    ssl_prefer_server_ciphers off;"
        print "    ssl_session_cache   shared:aranmed_tls:${GATEWAY_TLS_SESSION_CACHE};"
        print "    ssl_session_timeout ${GATEWAY_TLS_SESSION_TIMEOUT};"
        print "    ssl_session_tickets off;"
        print ""
        print "    add_header Strict-Transport-Security \"max-age=${GATEWAY_HSTS_MAX_AGE}\" always;"
        next
    }
    { print }
' "$SRC" > "$DEST.tmp"

# 2) Prepend the plain-HTTP redirect listener.
{
    cat <<'EOF'
# Injected by 10-aranmed-tls.sh because GATEWAY_TLS_ENABLED is set.
# Plain HTTP exists only to bounce clients to HTTPS.
server {
    listen ${GATEWAY_PORT};
    server_name _;

    # Keep the container healthcheck answerable without following a redirect.
    location = /healthz {
        access_log off;
        add_header Content-Type application/json always;
        return 200 '{"ok":true,"service":"aranmed-gateway","tls":true}';
    }

    location / {
        return 301 https://$host:${GATEWAY_TLS_PUBLISH_PORT}$request_uri;
    }
}

EOF
    cat "$DEST.tmp"
} > "$DEST"
rm -f "$DEST.tmp"

echo "10-aranmed-tls.sh: TLS enabled — HTTPS on \${GATEWAY_TLS_PORT}, HTTP redirects"
