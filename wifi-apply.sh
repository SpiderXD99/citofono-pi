#!/bin/sh
# Applica-sicuro: prova una rete Wi-Fi NUOVA; se entro ~45s non ha Internet, torna
# alla rete di prima. Avviato staccato da rete.py (sopravvive al cambio di rete).
#   wifi-apply.sh <ssid_nuovo> <password> <connessione_di_prima>
NEW="$1"; PW="$2"; OLD="$3"
logger -t citofono-wifi "provo rete '$NEW' (riserva: '${OLD:-nessuna}')"
if [ -n "$PW" ]; then
  nmcli device wifi connect "$NEW" password "$PW" >/dev/null 2>&1
else
  nmcli device wifi connect "$NEW" >/dev/null 2>&1
fi
sleep 45
conn=$(nmcli -t networking connectivity 2>/dev/null)
if [ "$conn" = "full" ]; then
  logger -t citofono-wifi "rete '$NEW' OK (connettivita' piena)"
else
  logger -t citofono-wifi "rete '$NEW' senza Internet ($conn): torno a '${OLD:-?}'"
  # non lasciare che la rete fallita si riprenda il comando da sola
  [ "$NEW" != "$OLD" ] && nmcli connection modify "$NEW" connection.autoconnect no >/dev/null 2>&1
  [ -n "$OLD" ] && nmcli connection up "$OLD" >/dev/null 2>&1
fi
