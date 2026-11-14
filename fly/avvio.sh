#!/bin/bash
# avvio.sh - processo principale del container su Fly.io.
#
# Avvia tre cose e resta in attesa:
#   frps         porta 7000: ci si collega xfrpc dal monitor. Le porte dei proxy
#                (placca 17080, web del monitor 17443) si aprono SOLO su 127.0.0.1:
#                le usa server.py, da Internet non si vedono.
#   cloudflared  tunnel verso Cloudflare (l'ingresso punta a https://localhost:443)
#   server.py    pagina dei PIN + admin, su 127.0.0.1:443 con certificato autofirmato
# Se frps o cloudflared si fermano, esce: Fly riavvia la macchina. server.py invece
# si rilancia da solo (dopo /aggiorna esce apposta per caricare il codice nuovo).
#
# Segreti (fly secrets set ...): FRP_TOKEN (obbligatorio, lo stesso di xfrpc sul
# monitor), TUNNEL_TOKEN (tunnel Cloudflare), ADMIN_PASS (facoltativo: password
# iniziale dell'utente "admin" del pannello, usata solo se non esiste ancora).
# Dati persistenti nel volume /data (account, configurazione, certificato).
set -u
D=/data
APP=$D/app
REPO_URL=${REPO_URL-https://github.com/SpiderXD99/citofono-pi.git}

log() { echo "[avvio] $*"; }

if ! [[ "${FRP_TOKEN:-}" =~ ^[A-Za-z0-9]{24,}$ ]]; then
	log "ERRORE: FRP_TOKEN mancante o non valido (solo lettere e cifre, almeno 24)."
	log "        fly secrets set FRP_TOKEN=\$(openssl rand -hex 24)"
	sleep 30
	exit 1
fi
mkdir -p "$D"
chmod 700 "$D"
umask 077

# 1. codice: clone del repository pubblico (git pull a ogni avvio e da /aggiorna);
#    con REPO_URL vuoto, o se il clone non riesce, la copia dentro l'immagine
if [ -z "$REPO_URL" ]; then
	rm -rf "$APP"
	cp -r /app "$APP"
elif [ -d "$APP/.git" ]; then
	git -C "$APP" pull --ff-only -q || log "git pull non riuscito: uso il codice gia' presente"
else
	rm -rf "$APP.nuovo"
	if git clone -q --depth 1 "$REPO_URL" "$APP.nuovo"; then
		rm -rf "$APP"
		mv "$APP.nuovo" "$APP"
	else
		rm -rf "$APP.nuovo"
		log "clone di $REPO_URL non riuscito"
	fi
fi
if [ ! -f "$APP/server.py" ]; then
	rm -rf "$APP"
	cp -r /app "$APP"
	log "uso il codice dell'immagine"
fi

# 2. certificato autofirmato per https://localhost:443 (il tunnel usa No TLS Verify)
if [ ! -s "$D/cert.pem" ] || [ ! -s "$D/key.pem" ]; then
	openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=localhost" \
		-keyout "$D/key.pem" -out "$D/cert.pem" 2>/dev/null || log "ERRORE: certificato non creato"
fi

# 3. configurazione del server: creata la prima volta, poi la cambia il pannello
if [ ! -f "$D/citofono.conf" ]; then
	cat > "$D/citofono.conf" <<EOF
# Citofono su Fly.io (creato da avvio.sh; si modifica anche dal pannello).
PIATTAFORMA=fly
# la placca e il web del monitor arrivano da xfrpc sul monitor, attraverso frps
PLACCA=127.0.0.1:17080
MONITOR=https://127.0.0.1:17443
PUBBLICO=
INDIRIZZO=127.0.0.1
PORTA=443
CERT=$D/cert.pem
CHIAVE_TLS=$D/key.pem
PAGINA=$APP/index.html
APP_DIR=$APP
ADMIN=$D/admin.json
UTENTI=$D/utenti.json
PLACCA_CRED=$D/placca.json
PROPRIETA=$D/proprieta.json
PONTE_CONF=$D/ponte.conf
TUNNEL_ENV=$D/tunnel.env
CLOUDFLARED=/usr/local/bin/cloudflared
EOF
	log "creato $D/citofono.conf"
fi

# 4. primo accesso: utente "admin" del pannello, se c'e' ADMIN_PASS e non esiste ancora
if [ ! -f "$D/admin.json" ] && [ ! -f "$D/utenti.json" ] && [ -n "${ADMIN_PASS:-}" ]; then
	printf '%s' "$ADMIN_PASS" | python3 -c "
import sys
sys.path.insert(0, '$APP')
import server as s
s.leggi_conf('$D/citofono.conf')
s.salva_admin('admin', sys.stdin.read())
" && log "creato l'utente admin del pannello"
fi

# 5. frps
cat > /run/frps.toml <<EOF
bindAddr = "0.0.0.0"
bindPort = 7000
proxyBindAddr = "127.0.0.1"
auth.method = "token"
auth.token = "$FRP_TOKEN"
# xfrpc del monitor si collega in TLS (verificato: nessun dato in chiaro sul filo);
# senza TLS si viene rifiutati. NON usare use_encryption in xfrpc: la sua cifratura
# non e' compatibile con frps 0.58 e blocca i dati (il login riesce lo stesso)
transport.tls.force = true
allowPorts = [ { start = 17080, end = 17080 }, { start = 17443, end = 17443 } ]
log.to = "console"
log.level = "info"
EOF
frps -c /run/frps.toml &

if [ -n "${TUNNEL_TOKEN:-}" ]; then
	cloudflared tunnel --no-autoupdate run &      # legge TUNNEL_TOKEN dall'ambiente
else
	log "TUNNEL_TOKEN non impostato: niente tunnel Cloudflare"
fi

(
	while true; do
		python3 "$APP/server.py" --conf "$D/citofono.conf"
		log "server.py uscito (codice $?): lo riavvio"
		sleep 1
	done
) &

wait -n
log "frps o cloudflared si sono fermati: esco, Fly riavvia la macchina"
exit 1
