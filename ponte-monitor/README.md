# Ponte applicativo sul monitor

Nei kit DNAKE/Nivian a **2 fili** la placca esterna non sta sulla rete di casa:
è raggiungibile solo dal monitor interno, tramite i 2 fili (rete `192.168.68.x`).
Il Raspberry, quindi, non può parlare direttamente con la placca.

`relay` risolve il problema senza toccare il firmware: è un piccolo inoltro TCP
che gira **sul monitor** (accesso di manutenzione via telnet, credenziali del
costruttore). Ascolta su una porta del monitor e inoltra ogni connessione alla
web API della placca. Il Pi parla solo con il monitor; il monitor gira tutto alla
placca, che già raggiunge.

```
telefono --tunnel--> Raspberry --LAN--> monitor(:8070) --2 fili--> placca(:80)
```

## File
- `relay.c` — sorgente (inoltro TCP minimale, un processo figlio per connessione).
- `relay` — binario **ARM statico** già compilato (gira sul monitor senza dipendenze).

Ricompilare: `arm-linux-gnueabihf-gcc -static -O2 -s -o relay relay.c`

## Uso
Sul monitor (dove `192.168.68.70` è l'IP della placca):

```sh
/tmp/relay 8070 192.168.68.70 80 &
```

Poi nel pannello admin del Pi si imposta come «IP placca» l'indirizzo del
monitor con la porta del relay, es. `192.168.1.22:8070`.

## Avvertenze
- L'accesso al monitor va ottenuto in modo legittimo (credenziali di manutenzione
  dal costruttore/installatore). Non esporre mai la porta del relay su Internet:
  dà accesso alla placca. Tienila in LAN; da fuori si passa solo dal tunnel
  autenticato del Pi.
- Il relay va reso persistente agganciandolo a una zona scrivibile del monitor
  che parte all'avvio (da definire sul modello specifico).
