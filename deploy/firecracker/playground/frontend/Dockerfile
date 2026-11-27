# Firecracker Playground frontend: static files only (build context: this directory).
# Deploy it on any host; set PLAYGROUND_API_BASE_URL to the backend URL.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js style.css config.template.js /usr/share/nginx/html/
COPY 40-playground-config.sh /docker-entrypoint.d/40-playground-config.sh
RUN chmod 0755 /docker-entrypoint.d/40-playground-config.sh && rm -f /usr/share/nginx/html/50x.html

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
