#!/bin/sh
# Installa la pagina dei PIN + pannello sul Raspberry Pi. Esegui dal clone del repo:
#   sudo sh installa.sh
# Idempotente: rilanciarlo aggiorna i servizi senza toccare la configurazione gia' fatta.
set -eu

APP="$(cd "$(dirname "$0")" && pwd)"
ETC=/etc/citofono

[ "$(id -u)" = 0 ] || { echo "Esegui con sudo: sudo sh installa.sh"; exit 1; }

echo "== pacchetti =="
apt-get update
apt-get install -y python3 git openssl curl ca-certificates

echo "== cloudflared =="
if [ ! -x /usr/bin/cloudflared ]; then
	case "$(dpkg --print-architecture)" in
		arm64) F=cloudflared-linux-arm64 ;;
		armhf) F=cloudflared-linux-arm ;;
		amd64) F=cloudflared-linux-amd64 ;;
		*)     F=cloudflared-linux-arm64 ;;
	esac
	echo "scarico $F ..."
	curl -fL "https://github.com/cloudflare/cloudflared/releases/latest/download/$F" -o /usr/bin/cloudflared
	chmod 755 /usr/bin/cloudflared
fi
/usr/bin/cloudflared --version || true

mkdir -p "$ETC"; chmod 755 "$ETC"

echo "== certificato del server (autofirmato) =="
if [ ! -s "$ETC/cert.pem" ]; then
	openssl req -x509 -newkey rsa:2048 -nodes -keyout "$ETC/key.pem" -out "$ETC/cert.pem" \
		-days 3650 -subj "/CN=citofono" >/dev/null 2>&1
	chmod 600 "$ETC/key.pem"; chmod 644 "$ETC/cert.pem"
fi

echo "== configurazione =="
if [ ! -s "$ETC/citofono.conf" ]; then
	cp "$APP/citofono.conf" "$ETC/citofono.conf"
	printf "IP della placca sulla tua rete (es. 192.168.1.90): "; read -r PLACCA
	[ -n "$PLACCA" ] && sed -i "s#^PLACCA=.*#PLACCA=$PLACCA#" "$ETC/citofono.conf"
	printf "Web del monitor [invio per saltare, es. https://192.168.1.80]: "; read -r MONITOR
	sed -i "s#^MONITOR=.*#MONITOR=${MONITOR}#" "$ETC/citofono.conf"
fi
# i percorsi dei file puntano sempre a questo clone (anche se reinstalli altrove)
sed -i "s#^PAGINA=.*#PAGINA=$APP/index.html#" "$ETC/citofono.conf"
sed -i "s#^APP_DIR=.*#APP_DIR=$APP#" "$ETC/citofono.conf"

echo "== password del pannello admin =="
if [ ! -s "$ETC/admin.json" ]; then
	printf "Utente del pannello [admin]: "; read -r AU; [ -n "$AU" ] || AU=admin
	while : ; do
		printf "Password del pannello (almeno 12 caratteri, non si vede): "
		stty -echo; read -r AP; stty echo; echo
		[ "${#AP}" -ge 12 ] && break
		echo "troppo corta, riprova."
	done
	python3 - "$AU" "$AP" "$ETC/admin.json" <<'PY'
import hashlib, json, os, secrets, sys
u, p, path = sys.argv[1], sys.argv[2], sys.argv[3]
salt = secrets.token_bytes(16)
h = hashlib.pbkdf2_hmac('sha256', p.encode('utf-8'), salt, 210000)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, json.dumps({'user': u, 'iter': 210000, 'salt': salt.hex(), 'hash': h.hex()}).encode())
os.close(fd)
PY
fi

# il token del tunnel si mette dal pannello; qui solo il file vuoto (600) per il servizio
[ -f "$ETC/tunnel.env" ] || { printf 'TUNNEL_TOKEN=\n' > "$ETC/tunnel.env"; chmod 600 "$ETC/tunnel.env"; }

echo "== servizi systemd =="
for U in citofono.service citofono-tunnel.service citofono-update.service citofono-update.timer ponte.service ponte.timer rete-hotspot.service rete-hotspot.timer; do
	sed "s#/opt/citofono#$APP#g" "$APP/$U" > "/etc/systemd/system/$U"
done
systemctl daemon-reload
systemctl enable --now citofono.service
systemctl enable --now citofono-update.timer
# il relay sul monitor viene (ri)avviato automaticamente ogni pochi minuti
systemctl enable --now ponte.timer
# guardia dell'hotspot di soccorso (si accende se il Pi resta senza rete)
chmod +x "$APP/wifi-apply.sh" "$APP/hotspot-guard.sh" 2>/dev/null || true
systemctl enable --now rete-hotspot.timer
# il tunnel si avvia dal pannello (serve prima il token)

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Fatto."
echo "  Pagina dei PIN:  https://${IP:-IP_DEL_PI}/data/pin/"
echo "  Pannello admin:  https://${IP:-IP_DEL_PI}/admin"
echo "Il browser avvisera' del certificato autofirmato: e' atteso, accetta."
echo "Nel pannello: incolla il token Cloudflare e premi Avvia per il tunnel."
