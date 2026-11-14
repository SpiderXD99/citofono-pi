# Bot Telegram per i codici della placca (sul monitor)

Gestisci da Telegram i codici di accesso della placca: crei un codice ospite con
scadenza, ne vedi l'elenco, lo togli. **Gira sul monitor**, con la sua shell e il suo
`wget`: niente Raspberry, niente server, niente IP pubblico, gratis.

Come l'app Tuya del monitor: è il monitor che chiama Telegram ("ci sono messaggi
per me?", long polling) e non il contrario. Quando scrivi al bot, il monitor esegue
il comando sulla web API della placca e ti risponde.

```
tu (Telegram) → server Telegram ← monitor (bot.sh, chiama lui) → placca (porta 80)
```

## Comandi

| comando | cosa fa |
|---|---|
| `/codici` | elenco dei codici sulla placca (numero, nome, PIN, scadenza, usi) |
| `/ospite Mario 3` | codice per 3 giorni da oggi, PIN casuale a 6 cifre |
| `/ospite Mario 3 x2` | come sopra, ma valido per 2 ingressi |
| `/ospite Mario 2026-12-24 2026-12-26` | con le date |
| `/permanente Casa` | codice senza scadenza (PIN casuale), o `/permanente Casa 4821` |
| `/elimina 2` o `/elimina Mario` | toglie un codice (il numero è quello di `/codici`) |
| `/stato` | il bot e la placca rispondono? quanta RAM libera? |
| `/id` | il tuo ID Telegram (serve per autorizzarti) |

Il bot risponde con il PIN e con un messaggio pronto da inoltrare all'ospite. Sulla
placca si digita il PIN e poi `#`. I codici ospite valgono tutto il giorno, tutti i
giorni, tra le due date.

## Sicurezza: cosa sapere

- Lo usano **solo gli ID Telegram in `GESTORI`**, e **solo in chat privata**: in un
  gruppo il bot si rifiuta, perché i PIN finirebbero in una chat condivisa.
- Fa **solo operazioni sui codici** della placca. Il testo dei messaggi non viene mai
  eseguito: nomi e numeri sono controllati carattere per carattere (provato con
  `$(...)`, `*`, `&campo=` e virgolette).
- La password della placca sta sul monitor solo come **md5** (la stessa forma che
  la placca vuole al login).
- **Limiti da conoscere:**
  - le chat con i bot non sono cifrate end-to-end: **Telegram vede i messaggi**,
    compresi i PIN creati;
  - il `wget` del monitor cifra la connessione ma **non verifica il certificato**:
    chi riuscisse a mettersi in mezzo potrebbe leggere i messaggi e rubare il token
    del bot. Anche con il token, però, non potrebbe andare oltre i comandi sui codici
    né fingersi un gestore (Telegram indica il mittente vero). Se succede: in
    @BotFather `/revoke` e nuovo token.
- Non esporre mai su Internet il web della placca o del monitor: il bot non ne ha
  bisogno.

## Installazione (dal telnet del monitor)

### 1. Crea il bot su Telegram
Scrivi a **@BotFather** → `/newbot` → scegli nome e username. Ti dà un **token**
(`123456789:AAH...`): è la chiave del bot, non darla a nessuno.

### 2. Copia i file sul monitor
Dal Pi o da un PC in casa, nella cartella `telegram` del repository:

```
python3 -m http.server 8000
```

Sul monitor (al posto di `IP_PC` metti l'IP del PC o del Pi):

```
mkdir -p /dnake/data/tgbot && cd /dnake/data/tgbot
wget -O bot.sh http://IP_PC:8000/bot.sh
wget -O config http://IP_PC:8000/config.esempio
chmod 700 bot.sh && chmod 600 config
```

### 3. Compila il config
Calcola l'md5 della password del web della placca:

```
printf '%s' 'LA_PASSWORD_DELLA_PLACCA' | md5sum
```

Poi scrivi i valori in `/dnake/data/tgbot/config` (con `vi`, oppure riga per riga
con `sed`):

```
sed -i 's/^TOKEN=.*/TOKEN=IL_TUO_TOKEN/' config
sed -i 's/^PLACCA_MD5=.*/PLACCA_MD5=LE_32_CIFRE_DELL_MD5/' config
```

`GESTORI` lo riempi al passo 5: prima ti serve il tuo ID.

### 4. Verifica

```
sh /dnake/data/tgbot/bot.sh verifica
```

Deve dire `comandi: ok`, `wget --post-data: ok`, `Telegram: ok, bot @...` e
`placca ...: login ok`. Se dice `date -d @... non funziona`, i codici ospite si creano
solo con le date (`/ospite Mario 2026-12-24 2026-12-26`).

### 5. Il tuo ID e la prima prova

```
sh /dnake/data/tgbot/bot.sh prova
```

Su Telegram scrivi `/id` al bot: ti risponde con il tuo ID. Ctrl+C, aggiungilo:

```
sed -i 's/^GESTORI=.*/GESTORI=IL_TUO_ID/' /dnake/data/tgbot/config
```

(più persone: `GESTORI=111111,222222`). Rilancia `prova`, scrivi `/codici`.

### 6. In background

```
sh /dnake/data/tgbot/bot.sh avvia
sh /dnake/data/tgbot/bot.sh stato
```

⚠ **Dopo un riavvio del monitor il bot non riparte da solo**: il firmware del monitor
non ha un posto dove aggiungere programmi all'avvio. Va rilanciato con `avvia`
finché non si sceglie un avvio automatico (sveglia dalla placca, oppure una riga
nell'`init` del monitor).

## Risorse

Uno script di shell più qualche `wget` ogni tanto: pochi MB di RAM, nessun file
grande. I dati stanno in `/dnake/data/tgbot` (config, ultimo messaggio letto), il log
in RAM in `/var/tgbot/log` (si accorcia da solo).

## Prove

`prova/banco.py` simula Telegram e la placca (login, sessione che scade, risposta
"in corso" con `action=3`) e controlla ogni comando, i rifiuti e gli input ostili:

```
python3 prova/banco.py --solo-busybox CARTELLA_CON_I_LINK_DI_BUSYBOX -- busybox sh bot.sh prova
```
