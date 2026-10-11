# Monitoraggio prestazioni EGroupware

Kit per capire **dove** EGroupware perde tempo, senza rallentarlo. È pensato per
l'installazione Docker standard (`/etc/egroupware-docker`, container `egroupware`
e `egroupware-db`).

## Cosa registra e quanto costa

| Fonte | Cosa registra | Costo |
|---|---|---|
| Access log di PHP-FPM | ogni richiesta PHP: inizio, durata, % CPU, memoria di picco, URL (quindi `menuaction`) | una scrittura di ~200 byte a fine richiesta, nell'ordine dei microsecondi |
| Slowlog di PHP-FPM | lo stack PHP di ogni richiesta che supera **3 s** (la funzione dove è ferma e il codice EGroupware che l'ha chiamata) | nullo sulle richieste normali. Su una richiesta già lenta il master la sospende per pochi millisecondi mentre legge lo stack |
| Slow query log di MariaDB | le query oltre **1 s** | nullo sulle query normali, scrive solo quelle lente |
| Campionatore (cron, 1 volta al minuto) | carico, CPU, iowait, steal, memoria e swap dell'host; worker FPM occupati, coda e `max children reached`; thread MariaDB, query in corso da oltre 5 s, lock InnoDB; CPU e RAM di ogni container; opzionale: tempo di connessione TCP+TLS verso IMAP o altri server | pochi secondi di un processo a priorità minima (`nice 19`, `ionice idle`), mai due in parallelo |

Nessun profiler (Xdebug, XHProf, Blackfire, APM) è installato nel codice: sono
quelli che rallentano. L'unica configurazione toccata è quella di PHP-FPM e di
MariaDB, tramite file aggiunti: l'immagine Docker e i sorgenti restano invariati.

Lo **slowlog** è la parte più utile. Per ogni richiesta lenta dice se stava
aspettando il database, il server IMAP, il disco, LDAP o il lock della
sessione PHP, oppure se stava calcolando. La % CPU dell'access log conferma la
diagnosi: una richiesta di 8 s con CPU al 2 % sta aspettando qualcosa, una
con CPU al 95 % sta calcolando.

## Installazione

Sul server Docker, da root:

```bash
git clone --depth 1 -b claude/lentezza-posta-diagnosi-piano-iq5swt https://github.com/CActor/egroupware.git /tmp/egw
cd /tmp/egw/doc/monitoring
./install.sh --probe "imaps://mail.example.org:993"
```

- `--probe` è facoltativo: elenco di URL, separati da spazi, di cui misurare la
  connessione ogni minuto (per esempio il server IMAP e quello SMTP).
- `--compose-dir DIR` serve se `docker-compose.yml` non è in `/etc/egroupware-docker`.
- Lo script chiede conferma prima di ricreare i container `egroupware` e `db`:
  è l'unico momento con qualche secondo di disservizio. Conviene farlo fuori orario.

Cosa installa:

- il kit in `/opt/egroupware-monitor`
- i log in `/var/log/egroupware-monitor` (leggibili solo da root, perché
  contengono gli URL delle richieste), ruotati ogni giorno e tenuti 30 giorni
  (`/etc/logrotate.d/egroupware-monitor`)
- `docker-compose.monitoring.yml` accanto a `docker-compose.yml`, aggiunto a
  `COMPOSE_FILE` in `.env`, così un normale `docker compose up -d` lo mantiene
  (di `.env` viene salvata una copia `.env.bak-egw-monitor-*`)
- il cron `/etc/cron.d/egroupware-monitor`

Il container `egroupware` riceve la capability `SYS_PTRACE`, necessaria allo
slowlog per leggere lo stack del processo PHP. Senza, il file `fpm-slow.log`
contiene solo errori `ptrace`.

### Database non in container

Se MariaDB/MySQL gira sull'host o su un altro server, l'installatore lo segnala
e salta la parte del database. Per attivare lo slow log, senza riavvio:

```sql
SET GLOBAL slow_query_log_file = '/var/log/egroupware-monitor/mariadb/mariadb-slow.log';
SET GLOBAL long_query_time = 1;
SET GLOBAL slow_query_log = 1;
```

Per renderlo permanente copiare le righe di `mariadb/egw-monitor.cnf` nella
configurazione del server. La cartella del log deve essere scrivibile
dall'utente `mysql`.

## Report

```bash
/opt/egroupware-monitor/egw-monitor-report.py --days 1
/opt/egroupware-monitor/egw-monitor-report.py --from "2026-10-12 08:00" --to "2026-10-12 12:00"
/opt/egroupware-monitor/egw-monitor-report.py --days 7 --slow-ms 1000 --top 40 > report.txt
```

Il report ha cinque sezioni:

1. **Richieste PHP**: quante sono lente, quali endpoint (`menuaction`, CalDAV/CardDAV,
   WebDAV, ActiveSync) consumano più tempo, con p50/p95/max, % CPU e memoria.
   Gli ActiveSync `Ping` sono esclusi perché restano aperti per progetto.
2. **Dove aspettano le richieste lente**: dallo slowlog, la categoria dell'attesa
   (database, IMAP, SMTP, LDAP, filesystem/VFS, lock sessione PHP, HTTP esterno,
   CPU) e i punti del codice EGroupware più frequenti.
3. **Query lente MariaDB**, raggruppate per forma (i valori diventano `?`/`N`).
4. **Andamento orario**: richieste e lentezze affiancate a worker FPM, coda,
   CPU, iowait, steal, load, thread del DB e lock, poi la CPU di ogni container
   e i tempi delle connessioni di prova.
5. **Indicazioni automatiche** ricavate dai dati.

Per l'analisi si possono condividere il testo del report o i file di
`/var/log/egroupware-monitor`.

## Come leggere i casi tipici

| Sintomo nel report | Causa probabile |
|---|---|
| Lente con CPU bassa, attesa `IMAP` | server di posta lento o cartelle molto grandi |
| Attesa `lock sessione PHP` | una richiesta lenta blocca le altre dello stesso utente (le richieste di un utente con la stessa sessione sono serializzate) |
| Attesa `database` + query nella sezione 3 | query o indice da ottimizzare, oppure lock InnoDB (colonna `lock`) |
| `coda` / `limite` > 0 | tutti i worker PHP occupati: le richieste aspettano prima di partire |
| `iowait` alto | disco saturo (backup, altri container, storage di rete) |
| `steal` alto | VM che aspetta CPU dall'hypervisor |
| CPU di `collabora`/`rocketchat` alta nelle stesse ore | un altro servizio sottrae risorse a EGroupware |
| Picchi ogni 5 minuti | job asincroni di EGroupware (cron `asyncwrapper.php`) |

## Disinstallazione

```bash
/tmp/egw/doc/monitoring/install.sh --uninstall
```

La disinstallazione rimuove il file compose, la voce in `COMPOSE_FILE`, il cron
e logrotate, poi ricrea i container. I log e `/opt/egroupware-monitor` restano
e vanno cancellati a mano.

## Note

- Se un aggiornamento dell'immagine cambia la versione di PHP (es. da 8.4 a 8.5),
  la configurazione FPM non viene più letta: EGroupware funziona normalmente ma
  `fpm-access.log` smette di crescere. Basta rilanciare `install.sh`.
- Gli orari dei log dei container sono in UTC; il report li mostra in ora locale.
- Spazio su disco: circa 20 MB per 100.000 richieste. Con la rotazione a 30 giorni
  compressi, normalmente meno di 1 GB.
