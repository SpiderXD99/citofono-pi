#!/bin/sh
# Guardia dell'hotspot di soccorso. Lanciata ogni minuto da rete-hotspot.timer.
#  - se il Pi e' collegato a una rete normale: assicura l'hotspot spento;
#  - se resta senza rete per qualche minuto: accende l'hotspot 'citofono-setup';
#  - se e' gia' sull'hotspot: ogni tanto lo spegne un attimo per riprovare a
#    rientrare su una rete conosciuta (recupera dai cali temporanei di Wi-Fi).
AP="citofono-setup"
CONTA=/run/citofono-hotspot.conta        # cicli consecutivi senza rete
RIPROVA=/run/citofono-hotspot.riprova    # cicli da fermo sull'hotspot
SOGLIA=3                                  # ~3 minuti senza rete -> accendi
OGNI=10                                   # ~10 minuti sull'hotspot -> riprova rete

attivo() { nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP"; }

# c'e' una connessione "normale" (non l'hotspot) su una scheda di rete vera?
collegato() {
  nmcli -t -f DEVICE,STATE,CONNECTION device status 2>/dev/null | awk -F: -v ap="$AP" '
    $2=="connected" && $3!="" && $3!="--" && $3!=ap && ($1 ~ /^(eth|wlan|usb|enx|end|wwan)/) {n++}
    END { exit !(n>0) }'
}

if collegato; then
  echo 0 > "$CONTA" 2>/dev/null; echo 0 > "$RIPROVA" 2>/dev/null
  if attivo; then
    logger -t citofono-hotspot "rete tornata: spengo l'hotspot"
    nmcli connection down "$AP" >/dev/null 2>&1
  fi
elif attivo; then
  r=$(cat "$RIPROVA" 2>/dev/null || echo 0); r=$((r + 1))
  if [ "$r" -ge "$OGNI" ]; then
    echo 0 > "$RIPROVA" 2>/dev/null
    logger -t citofono-hotspot "provo a rientrare su una rete conosciuta"
    nmcli connection down "$AP" >/dev/null 2>&1
    sleep 25
    if ! collegato; then nmcli connection up "$AP" >/dev/null 2>&1; fi
  else
    echo "$r" > "$RIPROVA" 2>/dev/null
  fi
else
  c=$(cat "$CONTA" 2>/dev/null || echo 0); c=$((c + 1)); echo "$c" > "$CONTA" 2>/dev/null
  if [ "$c" -ge "$SOGLIA" ]; then
    echo 0 > "$RIPROVA" 2>/dev/null
    logger -t citofono-hotspot "nessuna rete da $c controlli: accendo l'hotspot"
    nmcli connection up "$AP" >/dev/null 2>&1
  fi
fi
