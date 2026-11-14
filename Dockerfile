# Citofono su Fly.io: server.py (pagina PIN + admin) + frps (lo raggiunge xfrpc
# dal monitor) + cloudflared (citofono.<dominio> su Cloudflare). Vedi FLY.md.
# I segreti NON stanno qui: FRP_TOKEN e TUNNEL_TOKEN con "fly secrets set".
FROM python:3.12-slim

ARG FRP_VERSION=0.58.1
ARG CLOUDFLARED_VERSION=2026.11.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssl ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# frps: stessa serie (0.58) del protocollo di xfrpc sul monitor; verificato col
# file di checksum ufficiale della release
RUN set -eux; cd /tmp; \
    f="frp_${FRP_VERSION}_linux_amd64"; \
    curl -fsSLO "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${f}.tar.gz"; \
    curl -fsSLO "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_sha256_checksums.txt"; \
    grep " ${f}.tar.gz\$" frp_sha256_checksums.txt | sha256sum -c -; \
    tar xzf "${f}.tar.gz"; \
    install -m 755 "${f}/frps" /usr/local/bin/frps; \
    curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-amd64"; \
    chmod 755 /usr/local/bin/cloudflared; \
    rm -rf /tmp/*

# copia di riserva del codice: avvio.sh usa il clone del repository in /data/app
# (aggiornabile da /aggiorna) e ripiega su questa se il clone non riesce
COPY server.py ponte.py rete.py index.html icona-180.png icona-192.png icona-512.png /app/
COPY fly/avvio.sh /usr/local/bin/avvio.sh
RUN chmod 755 /usr/local/bin/avvio.sh

ENV PYTHONUNBUFFERED=1
CMD ["/usr/local/bin/avvio.sh"]
