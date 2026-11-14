#!/bin/sh
# bot.sh - bot Telegram per i codici della placca. Gira SUL MONITOR, con la sola
# shell di busybox e wget: nessun server esterno, nessun IP pubblico. Il monitor
# chiede a Telegram "ci sono messaggi?" (long polling, come fa l'app Tuya col suo
# cloud), esegue il comando verso la web API della placca e risponde in chat.
#
#   sh bot.sh verifica   controlla comandi disponibili, token e password della placca
#   sh bot.sh prova      in primo piano, con i messaggi (Ctrl+C per uscire)
#   sh bot.sh avvia      in background (dopo ogni riavvio del monitor)
#   sh bot.sh ferma | stato
#
# Configurazione in /dnake/data/tgbot/config (permessi 600): vedi config.esempio.
# Solo gli ID Telegram elencati in GESTORI possono usarlo, e solo in chat privata.
# Verso la placca passano solo le operazioni sui codici (api=user): nessun comando
# ricevuto da Telegram viene mai eseguito come shell.

D=${TGBOT_DIR:-/dnake/data/tgbot}
V=${TGBOT_TMP:-/var/tgbot}
CONF=$D/config
PIDF=$V/bot.pid
LOG=$V/log
TAB=$(printf '\t')
set -f				# niente espansione di * e ? nei testi ricevuti
PATH=${TGBOT_PATH:-/bin:/sbin:/usr/bin:/usr/sbin:$PATH}
mkdir -p "$V" 2>/dev/null
umask 077

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# ---------- configurazione (chiave=valore: letta, mai eseguita) ----------
conf() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONF" 2>/dev/null | head -n 1 | tr -d "\r\"'"; }

carica_conf() {
	[ -r "$CONF" ] || { echo "manca $CONF (copia config.esempio e compilalo)"; return 1; }
	TOKEN=$(conf TOKEN)
	GESTORI=$(conf GESTORI | tr ',;' '  ')
	PLACCA_MD5=$(conf PLACCA_MD5 | tr 'A-F' 'a-f')
	PLACCA=$(conf PLACCA)
	RELE=$(conf RELE); [ -n "$RELE" ] || RELE=0
	API=$(conf TG_API); [ -n "$API" ] || API=https://api.telegram.org
	case "$TOKEN" in [0-9]*:*) ;; *) echo "TOKEN mancante o non valido"; return 1 ;; esac
	case "$TOKEN" in *[!0-9A-Za-z:_-]*) echo "TOKEN non valido"; return 1 ;; esac
	case "$PLACCA_MD5" in *[!0-9a-f]*|'') echo "PLACCA_MD5 mancante o non valido"; return 1 ;; esac
	[ ${#PLACCA_MD5} -eq 32 ] || { echo "PLACCA_MD5 deve essere di 32 caratteri"; return 1; }
	case "$RELE" in *[!0-9\;]*) echo "RELE non valido (es. 0 oppure 0;1)"; return 1 ;; esac
	[ -n "$GESTORI" ] || { echo "GESTORI vuoto: scrivi almeno il tuo ID Telegram (lo dice il comando /id)"; return 1; }
	for g in $GESTORI; do case "$g" in *[!0-9]*) echo "GESTORI: '$g' non e' un ID numerico"; return 1 ;; esac; done
	if [ -z "$PLACCA" ]; then	# come api.cgi: l'IP della placca abbinata, da sys.xml
		PLACCA=$(tr -d '\r' < /dnake/data/sys.xml 2>/dev/null |
			sed -n '/<door>[[:space:]]*$/,/<\/door>/s/.*<ip_0>\([0-9.]*\)<\/ip_0>.*/\1/p' | head -n 1)
	fi
	case "$PLACCA" in ''|*[!0-9.:]*) echo "IP della placca non trovato: scrivilo in PLACCA nel config"; return 1 ;; esac
	return 0
}

# ---------- Telegram ----------
# percent-encoding di un testo (anche su piu' righe) per i campi form
enc() {
	printf '%s' "$1" | awk 'BEGIN { for (i = 1; i < 256; i++) o[sprintf("%c", i)] = i; ORS = "" }
		{ if (NR > 1) printf "%%0A"
		  n = length($0)
		  for (i = 1; i <= n; i++) { c = substr($0, i, 1)
			if (c ~ /[A-Za-z0-9._~-]/) printf "%s", c; else printf "%%%02X", o[c] } }'
}

# $1 = metodo, $2 = dati form gia' codificati, $3 = timeout. Risposta JSON su stdout.
# Nota: il wget di busybox non verifica il certificato TLS (cifra, ma non controlla
# con chi parla): per questo il bot accetta comandi solo dai GESTORI e fa solo
# operazioni sui codici.
tg() { wget -q -T "${3:-20}" -O - --post-data "$2" "$API/bot$TOKEN/$1" 2>/dev/null; }

rispondi() {			# $1 = chat, $2 = testo
	tg sendMessage "chat_id=$1&disable_web_page_preview=true&text=$(enc "$2")" > /dev/null ||
		log "invio a $1 non riuscito"
}

# dagli aggiornamenti JSON di getUpdates: una riga per messaggio
#   update_id <TAB> id mittente <TAB> id chat <TAB> testo
analizza() {
	awk '{ s = s $0 }
	END {
		n = split(s, U, /"update_id":[ ]*/)
		for (i = 2; i <= n; i++) {
			u = U[i]; id = u; sub(/[^0-9].*/, "", id)
			da = ""; if (match(u, /"from":[ ]*[{][ ]*"id":[ ]*[0-9]+/)) { da = substr(u, RSTART, RLENGTH); sub(/.*[: ]/, "", da) }
			chat = ""; if (match(u, /"chat":[ ]*[{][ ]*"id":[ ]*-?[0-9]+/)) { chat = substr(u, RSTART, RLENGTH); sub(/.*[: ]/, "", chat) }
			# l ultimo "text" e quello del messaggio (un eventuale messaggio citato viene prima)
			t = ""; r = u
			while (match(r, /"text":[ ]*"([^"\\]|\\.)*"/)) {
				t = substr(r, RSTART, RLENGTH); r = substr(r, RSTART + RLENGTH)
				sub(/^"text":[ ]*"/, "", t); sub(/"$/, "", t)
			}
			gsub(/\t/, " ", t); gsub(/\\n/, " ", t); gsub(/\\"/, "\"", t); gsub(/\\\//, "/", t)
			print id "\t" da "\t" chat "\t" t
		}
	}'
}

# ---------- placca (stesse chiamate di api.cgi e della pagina) ----------
SID=
# $1 = percorso, $2 = dati POST (vuoto = GET). Corpo su stdout.
# ritorna 0 ok, 1 = 401, 2 = non risponde, 3 = 404, 4 = 503 (bloccata ~2 min dopo una password sbagliata)
richiesta() {
	_url="http://$PLACCA$1"; _dati=$2; _err=$V/wget.$$
	set -- -q -T 15 -O -
	[ -n "$SID" ] && set -- "$@" --header "Cookie: SessionID=$SID"
	[ -n "$_dati" ] && set -- "$@" --post-data "$_dati"
	wget "$@" "$_url" 2>"$_err"
	_rc=$?
	_e=$(cat "$_err" 2>/dev/null); rm -f "$_err"
	[ $_rc -eq 0 ] && return 0
	case "$_e" in *" 401"*) return 1 ;; *" 404"*) return 3 ;; *" 503"*) return 4 ;; esac
	return 2
}

accedi() {
	SID=
	_out=$(richiesta "/cgi-bin/webapi.cgi?api=login&username=admin&password=$PLACCA_MD5" "") || return $?
	SID=$(printf '%s' "$_out" | tr -d '\r\n' | sed -n 's/.*"SessionID"[[:space:]]*:[[:space:]]*"\{0,1\}\([0-9A-Za-z]*\).*/\1/p')
	[ -n "$SID" ] || return 1
}

# come richiesta(), rientrando una volta se la sessione e' scaduta
placca() {
	_giro=0
	while [ $_giro -lt 2 ]; do
		_giro=$((_giro + 1))
		if [ -z "$SID" ]; then accedi || return $?; fi
		_o=$(richiesta "$1" "$2"); _r=$?
		if [ $_r -eq 1 ]; then SID=; continue; fi
		printf '%s' "$_o"
		return $_r
	done
	return 1
}

esito() { printf '%s' "$1" | tr -d '\r\n' | sed -n 's/.*"result"[[:space:]]*:[[:space:]]*"\{0,1\}\(-\{0,1\}[0-9]*\).*/\1/p' | head -n 1; }

problema() {			# $1 = codice di ritorno di placca()
	case "$1" in
	1) echo "Password della placca non accettata: controlla PLACCA_MD5 nel config." ;;
	4) echo "La placca ha bloccato gli accessi per qualche minuto (password sbagliata): riprova tra 2-3 minuti." ;;
	3) echo "La placca non riconosce la richiesta." ;;
	*) echo "La placca ($PLACCA) non risponde." ;;
	esac
}

# scrittura su api=user: se la placca risponde 100 ("in corso") si chiede action=3
# finche' non ha finito, come fa la sua web app. Stampa il risultato numerico.
scrivi() {
	_o=$(placca "/cgi-bin/webapi.cgi?api=user" "$1") || return $?
	_n=$(esito "$_o"); _i=0
	while [ "$_n" = 100 ] && [ $_i -lt 60 ]; do
		sleep 1; _i=$((_i + 1))
		_o=$(placca "/cgi-bin/webapi.cgi?api=user" "action=3") || return $?
		_n=$(esito "$_o")
	done
	echo "${_n:-0}"
}

testo_esito() {
	case "$1" in
	0|'') return 0 ;;
	100) echo "La placca non ha confermato in tempo: controlla con /codici." ;;
	-1) echo "La placca ha rifiutato l'operazione." ;;
	-2) echo "Impossibile salvare: elenco pieno o dati non validi." ;;
	-3) echo "Date o orari non validi per la placca." ;;
	*) echo "Errore della placca ($1)." ;;
	esac
	return 1
}

# elenco delle persone in $V/elenco: una riga per persona, preceduta dal suo
# indice sulla placca (le righe vuote contano: gli indici restano quelli della placca)
#   indice <TAB> riga CSV di users.xml
carica_elenco() {
	placca "/cgi-bin/webapi.cgi?api=user" "" > /dev/null || return $?	# la placca rigenera users.xml
	_x=$(placca "/data/users.xml" ""); _r=$?
	[ $_r -eq 3 ] && _x=			# nessun file = nessuna persona
	[ $_r -eq 0 ] || [ $_r -eq 3 ] || return $_r
	printf '%s\n' "$_x" | tr -d '\r' | awk '{ if ($0 ~ /[^[:space:]]/) print (NR - 1) "\t" $0 }' > "$V/elenco"
}
# colonne di users.xml (verificate sulla placca):
#  1 nome 2 gruppo 3 stanza 4 tipo 5 stato 6 rele 7 tessere 8 pin_mode 9 pin
#  10 pass_on 11 usi 12 valid_on 13 dal 14 al 15 dalle 16 alle 17 settimana

# ---------- comandi ----------
AIUTO='Comandi:
/codici - elenco dei codici sulla placca
/ospite NOME GIORNI [xUSI] - codice temporaneo con PIN casuale (es. /ospite Mario 3 x2)
/ospite NOME DAL AL [xUSI] - con le date (es. /ospite Mario 2026-12-24 2026-12-26)
/permanente NOME [PIN] - codice senza scadenza (PIN casuale se manca)
/elimina N oppure /elimina NOME - toglie un codice (N = numero in /codici)
/stato - stato del bot e della placca
/id - il tuo ID Telegram

Sulla placca si digita il PIN e poi #. NOME: lettere, cifre, . _ - (senza spazi).'

cmd_codici() {
	carica_elenco || { problema $?; return; }
	_n=$(wc -l < "$V/elenco" | tr -d ' ')
	[ "$_n" -gt 0 ] || { echo "Nessun codice sulla placca."; return; }
	echo "Codici sulla placca ($_n):"
	awk -F'\t' '{ split($2, c, ","); for (k in c) gsub(/^[ \t]+|[ \t]+$/, "", c[k])
		r = ($1 + 1) ". " c[1]
		if (c[9] != "" && c[9] != "None") r = r " - PIN " c[9]
		if (c[5] != "1") r = r " - DISATTIVO"
		if (c[12] == "1") r = r " - dal " c[13] " al " c[14] (c[15] == "00:00" && c[16] == "23:59" ? "" : ", " c[15] "-" c[16])
		if (c[10] == "1") r = r " - usi rimasti " c[11]
		print r }' "$V/elenco" | head -n 80
	[ "$_n" -gt 80 ] && echo "(mostrati i primi 80)"
}

nome_valido() { case "$1" in ''|*[!A-Za-z0-9._-]*) return 1 ;; esac; [ ${#1} -le 24 ]; }
data_valida() { case "$1" in [12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]) return 0 ;; esac; return 1; }
oggi() { date +%Y-%m-%d; }
tra_giorni() { date -d "@$(( $(date +%s) + 86400 * $1 ))" +%Y-%m-%d 2>/dev/null; }

pin_casuale() {			# 6 cifre non gia' usate da un'altra persona
	_t=0
	while [ $_t -lt 20 ]; do
		_t=$((_t + 1))
		_c=$(od -An -N4 -tu4 /dev/urandom 2>/dev/null | tr -dc '0-9')
		[ -n "$_c" ] || _c=$(awk 'BEGIN { srand(); printf "%d", rand() * 4294967295 }')
		# modulo in awk: nella shell a 32 bit di busybox 4294967295 andrebbe in overflow
		_p=$(echo "$_c" | awk '{ printf "%06d", $1 % 1000000 }')
		case "$_p" in 000000|111111|123456|654321) continue ;; esac
		awk -F'\t' -v p="$_p" '{ split($2, c, ","); gsub(/ /, "", c[9]); if (c[9] == p) f = 1 } END { exit !f }' "$V/elenco" 2>/dev/null || { echo "$_p"; return 0; }
	done
	return 1
}

pin_usato() { awk -F'\t' -v p="$1" '{ split($2, c, ","); gsub(/ /, "", c[9]); if (c[9] == p) f = 1 } END { exit !f }' "$V/elenco" 2>/dev/null; }

# crea una persona; $1 nome $2 pin $3 dal $4 al (vuoti = senza scadenza) $5 usi (vuoto = illimitati)
crea() {
	_d="action=1&group=-1&name=$1&room=&relays=$(enc "$RELE")&status=1&type=0&cards=&pin_mode=1&pin_code=$2"
	if [ -n "$5" ]; then _d="$_d&pass_enable=1&pass_num=$5"; else _d="$_d&pass_enable=0"; fi
	if [ -n "$3" ]; then
		_d="$_d&active_enable=1&start_date=$3&end_date=$4&start_time=00%3A00&end_time=23%3A59&week=1%3B1%3B1%3B1%3B1%3B1%3B1"
	else
		_d="$_d&active_enable=0"
	fi
	_n=$(scrivi "$_d") || { problema $?; return 1; }
	testo_esito "$_n"
}

cmd_ospite() {			# NOME GIORNI [xN] | NOME DAL AL [xN]
	_nome=$1; shift 2>/dev/null
	nome_valido "$_nome" || { echo "Uso: /ospite NOME GIORNI [xUSI]  oppure  /ospite NOME DAL AL [xUSI]"; echo "NOME: lettere, cifre, . _ - (senza spazi, max 24)."; return; }
	_usi=; _dal=; _al=
	case "$#:$1" in
	0:) _giorni=1 ;;
	*:[0-9]|*:[0-9][0-9]|*:[0-9][0-9][0-9]) _giorni=$1; shift ;;
	*:x*) _giorni=1 ;;
	*) if data_valida "$1" && data_valida "$2"; then _dal=$1; _al=$2; shift 2
	   else echo "Non capisco la durata: scrivi i giorni (es. 3) o due date AAAA-MM-GG."; return; fi ;;
	esac
	case "$1" in
	'') ;;
	x[0-9]|x[0-9][0-9]) _usi=${1#x}; [ "$_usi" -ge 1 ] || { echo "Usi: almeno 1."; return; } ;;
	*) echo "Il numero di usi si scrive cosi': x2 (massimo 99)."; return ;;
	esac
	if [ -z "$_dal" ]; then
		[ "$_giorni" -ge 1 ] && [ "$_giorni" -le 366 ] || { echo "Giorni: da 1 a 366."; return; }
		_dal=$(oggi); _al=$(tra_giorni $((_giorni - 1)))
		[ -n "$_al" ] || { echo "Il monitor non sa calcolare le date: usa /ospite NOME DAL AL."; return; }
	fi
	[ "$(echo "$_al" | sed 's/-//g')" -ge "$(echo "$_dal" | sed 's/-//g')" ] || { echo "La data di fine viene prima di quella di inizio."; return; }
	carica_elenco || { problema $?; return; }
	_pin=$(pin_casuale) || { echo "Non riesco a generare un PIN libero."; return; }
	crea "$_nome" "$_pin" "$_dal" "$_al" "$_usi" || return
	log "creato codice ospite $_nome ($_dal-$_al) da $UID_ORA"
	echo "Codice creato per $_nome: PIN $_pin"
	echo "Valido dal $_dal al $_al (tutto il giorno)${_usi:+, per $_usi ingressi}."
	echo
	echo "Da inoltrare:"
	echo "Ciao $_nome, per aprire digita $_pin e poi # sulla tastiera del citofono. Vale dal $_dal al $_al${_usi:+, per $_usi ingressi}."
}

cmd_permanente() {		# NOME [PIN]
	nome_valido "$1" || { echo "Uso: /permanente NOME [PIN]  (PIN da 4 a 8 cifre; casuale se manca)"; return; }
	carica_elenco || { problema $?; return; }
	if [ -n "$2" ]; then
		case "$2" in *[!0-9]*) echo "Il PIN e' fatto solo di cifre (da 4 a 8)."; return ;; esac
		[ ${#2} -ge 4 ] && [ ${#2} -le 8 ] || { echo "Il PIN deve avere da 4 a 8 cifre."; return; }
		pin_usato "$2" && { echo "Questo PIN e' gia' usato da un altro codice."; return; }
		_pin=$2
	else
		_pin=$(pin_casuale) || { echo "Non riesco a generare un PIN libero."; return; }
	fi
	crea "$1" "$_pin" "" "" "" || return
	log "creato codice permanente $1 da $UID_ORA"
	echo "Codice permanente creato per $1: PIN $_pin (poi #)."
}

cmd_elimina() {			# N | NOME
	[ -n "$1" ] || { echo "Uso: /elimina N (numero in /codici) oppure /elimina NOME"; return; }
	carica_elenco || { problema $?; return; }
	case "$1" in
	*[!0-9]*)
		nome_valido "$1" || { echo "Nome non valido."; return; }
		_righe=$(awk -F'\t' -v n="$1" '{ split($2, c, ","); gsub(/^ +| +$/, "", c[1]); if (tolower(c[1]) == tolower(n)) print $1 }' "$V/elenco")
		_quanti=$(printf '%s\n' "$_righe" | grep -c .)
		[ "$_quanti" -eq 1 ] || { [ "$_quanti" -eq 0 ] && echo "Nessun codice con il nome $1." || echo "Ci sono $_quanti codici con il nome $1: usa il numero (/codici)."; return; }
		_idx=$_righe ;;
	*)
		_idx=$(awk -F'\t' -v n="$1" 'NR == n { print $1 }' "$V/elenco")
		[ -n "$_idx" ] || { echo "Non c'e' il codice numero $1: guarda /codici."; return; } ;;
	esac
	_nome=$(awk -F'\t' -v i="$_idx" '$1 == i { split($2, c, ","); gsub(/^ +| +$/, "", c[1]); print c[1] }' "$V/elenco")
	_n=$(scrivi "action=0&group=-1&index=$_idx") || { problema $?; return; }
	testo_esito "$_n" || return
	log "eliminato codice $_nome (indice $_idx) da $UID_ORA"
	echo "Eliminato il codice di $_nome: non apre piu'."
}

cmd_stato() {
	echo "Bot attivo sul monitor da $(( ($(date +%s) - AVVIO) / 60 )) minuti."
	echo "RAM libera: $(( $(sed -n 's/^MemAvailable:[[:space:]]*\([0-9]*\).*/\1/p' /proc/meminfo 2>/dev/null || echo 0) / 1024 )) MB"
	if carica_elenco; then
		echo "Placca $PLACCA: raggiungibile, $(wc -l < "$V/elenco" | tr -d ' ') codici."
	else
		problema $?
	fi
}

autorizzato() { for g in $GESTORI; do [ "$g" = "$1" ] && return 0; done; return 1; }

gestisci() {			# $1 = mittente, $2 = chat, $3 = testo
	UID_ORA=$1
	set -- "$1" "$2" $3	# le parole del testo diventano $3, $4, ... (set -f: niente glob)
	_da=$1; _chat=$2; shift 2
	_c=${1%%@*}; [ $# -gt 0 ] && shift
	case "$_c" in
	/id) rispondi "$_chat" "Il tuo ID Telegram e' $_da"; return ;;
	esac
	if ! autorizzato "$_da"; then
		log "rifiutato $_da: $_c"
		rispondi "$_chat" "Non sei autorizzato. Il tuo ID Telegram e' $_da: chiedi al gestore di aggiungerlo."
		return
	fi
	if [ "$_chat" != "$_da" ]; then	# niente gruppi: i PIN non devono finire in una chat condivisa
		rispondi "$_chat" "Usa il bot in chat privata."
		return
	fi
	case "$_c" in
	/start|/aiuto|/help) _r=$AIUTO ;;
	/codici) _r=$(cmd_codici) ;;
	/ospite) _r=$(cmd_ospite "$@") ;;
	/permanente) _r=$(cmd_permanente "$@") ;;
	/elimina) _r=$(cmd_elimina "$@") ;;
	/stato) _r=$(cmd_stato) ;;
	*) _r="Comando sconosciuto.

$AIUTO" ;;
	esac
	rispondi "$_chat" "$_r"
}

COMANDI='[{"command":"codici","description":"Elenco dei codici"},{"command":"ospite","description":"Codice temporaneo: NOME GIORNI [xUSI]"},{"command":"permanente","description":"Codice senza scadenza: NOME [PIN]"},{"command":"elimina","description":"Togli un codice: N oppure NOME"},{"command":"stato","description":"Stato del bot e della placca"},{"command":"id","description":"Il tuo ID Telegram"}]'

ciclo() {
	carica_conf || exit 1
	AVVIO=$(date +%s)
	log "bot avviato (placca $PLACCA)"
	tg setMyCommands "commands=$(enc "$COMANDI")" > /dev/null
	_off=$(cat "$D/offset" 2>/dev/null); case "$_off" in ''|*[!0-9]*) _off=0 ;; esac
	_errori=0
	while :; do
		_r=$(tg getUpdates "offset=$_off&timeout=50&allowed_updates=%5B%22message%22%5D" 70)
		case "$_r" in
		*'"ok":true'*|*'"ok": true'*) _errori=0 ;;
		*)	_errori=$((_errori + 1)); _p=$((_errori * 10)); [ $_p -gt 300 ] && _p=300
			log "Telegram non risponde (tentativo $_errori), riprovo tra ${_p}s"
			sleep $_p; continue ;;
		esac
		printf '%s' "$_r" | analizza > "$V/aggiornamenti"
		while IFS="$TAB" read -r _u _da _chat _testo; do
			case "$_u" in ''|*[!0-9]*) continue ;; esac
			# l'offset si salva PRIMA di eseguire: un comando che si blocca non si ripete per sempre
			_off=$((_u + 1)); echo "$_off" > "$D/offset"
			case "$_da:$_chat" in *[!0-9:-]*|:*|*:) continue ;; esac
			[ -n "$_testo" ] || continue
			gestisci "$_da" "$_chat" "$_testo"
		done < "$V/aggiornamenti"
		# il log sta in RAM (/var): non deve crescere senza limite
		[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 200000 ] && { tail -n 300 "$LOG" > "$LOG.1"; mv "$LOG.1" "$LOG"; }
	done
}

in_esecuzione() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

case "$1" in
ciclo) ciclo ;;
prova)
	LOG=/dev/stderr
	carica_conf || exit 1
	in_esecuzione && { echo "il bot gira gia' in background (pid $(cat "$PIDF")): prima 'sh $0 ferma'"; exit 1; }
	echo "bot in primo piano (Ctrl+C per uscire); scrivigli /id su Telegram"
	ciclo ;;
avvia)
	carica_conf || exit 1
	in_esecuzione && { echo "gia' attivo (pid $(cat "$PIDF"))"; exit 0; }
	_s=; command -v setsid > /dev/null 2>&1 && _s=setsid
	_io=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
	( $_s sh "$_io" ciclo < /dev/null >> "$LOG" 2>&1 & echo $! > "$PIDF" )
	sleep 2
	if in_esecuzione; then echo "avviato (pid $(cat "$PIDF")); log in $LOG"; else echo "non partito: guarda $LOG o usa 'sh $0 prova'"; exit 1; fi ;;
ferma)
	in_esecuzione || { echo "non era attivo"; rm -f "$PIDF"; exit 0; }
	kill "$(cat "$PIDF")"; sleep 1; rm -f "$PIDF"; echo "fermato" ;;
stato)
	if in_esecuzione; then echo "attivo (pid $(cat "$PIDF"))"; else echo "non attivo"; fi
	[ -f "$LOG" ] && tail -n 5 "$LOG" ;;
verifica)
	_manca=
	for c in sh wget awk sed grep tr head tail cut wc date sleep od mkdir; do
		command -v $c > /dev/null 2>&1 || _manca="$_manca $c"
	done
	[ -z "$_manca" ] && echo "comandi: ok" || echo "comandi mancanti:$_manca (od e' facoltativo)"
	wget --help 2>&1 | grep -q -- '--post-data' && echo "wget --post-data: ok" || echo "wget senza --post-data: il bot non puo' funzionare"
	[ -n "$(tra_giorni 1)" ] && echo "date -d @...: ok ($(oggi) -> $(tra_giorni 1))" || echo "date -d @... non funziona: usa /ospite NOME DAL AL"
	carica_conf || exit 1
	_me=$(tg getMe "" 20)
	case "$_me" in *'"ok":true'*|*'"ok": true'*) echo "Telegram: ok, bot @$(printf '%s' "$_me" | sed -n 's/.*"username":[ ]*"\([^"]*\)".*/\1/p')" ;;
	*) echo "Telegram: non raggiungibile o TOKEN sbagliato" ;; esac
	if accedi; then echo "placca $PLACCA: login ok"; else problema $?; fi ;;
*)
	echo "uso: sh $0 verifica|prova|avvia|ferma|stato"
	exit 1 ;;
esac
