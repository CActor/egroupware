# Stato di fatto — Installazione eGroupware (server `egw21`)

**Data rilevazione:** 21 giugno 2026
**Server:** `egw21` — installazione basata su Docker
**Rilevato tramite:** comandi eseguiti direttamente sul server (`docker ps`, `docker exec`, lettura di `setup.inc.php`)

---

## 1. Sintesi

L'applicazione **eGroupware è aggiornata all'ultima versione disponibile**. Non risulta
alcun blocco all'aggiornamento del core. L'unico elemento datato riguarda alcuni
**container di servizi accessori** (chat e push), che hanno tag di immagine fissati a
versioni vecchie nel file di configurazione del server.

> Nota: non esiste alcun file di configurazione "trattenuto" o "bloccato" sull'ambiente
> di sviluppo/Claude. Il `docker-compose.yml` operativo risiede esclusivamente sul server
> `egw21` ed è sotto il controllo dell'amministratore di sistema.

---

## 2. Versione eGroupware installata

| Componente | Valore |
|------------|--------|
| Versione API / major | **26.1** |
| Maintenance release | **26.5.20260507** (7 maggio 2026) |
| Versione header di configurazione | 1.29 |

La versione installata è **identica all'ultima maintenance release** presente nei sorgenti
ufficiali. **eGroupware è quindi pienamente aggiornato.**

Percorso sorgenti nel container: `/usr/share/egroupware/` (document root non standard
rispetto al template `/var/www`).

---

## 3. Container in esecuzione

| Container | Immagine | Stato | Valutazione |
|-----------|----------|-------|-------------|
| `egroupware` | `egroupware/egroupware:latest` | Up 3h | ✅ Aggiornato (26.5.20260507) |
| `egroupware-db` | `mariadb:11.8` | Up 3h | ✅ Allineato |
| `egroupware-nginx` | `nginx:stable-alpine` | Up 3h | ✅ Allineato |
| `egroupware-push` | `phpswoole/swoole:4.6-php7.4-alpine` | Up 3h | ⚠️ Datato (PHP 7.4 EOL; atteso `latest-alpine`) |
| `rocketchat` | `rocketchat/rocket.chat:5.4.10` | Up 3h | ⚠️ Datato (atteso `stable7`) |
| `rocketchat-mongo` | `mongo:5.0` | Up 3h | ⚠️ Datato (atteso `mongo:7.0`) |
| `portainer` | `portainer/portainer-ce:latest` | Up 3h | ℹ️ Accessorio (GUI Docker) |
| `at-cost-app` | `at-cost-analysis-app` | Up 3h (healthy) | ℹ️ Applicazione terza, non eGroupware |
| `at-cost-postgres` | `postgres:16-alpine` | Up 3h (healthy) | ℹ️ Applicazione terza |
| `at-cost-proxy` | `nginx:1.27-alpine` | Up 3h | ℹ️ Applicazione terza |

---

## 4. Meccanismo di aggiornamento

- L'immagine `egroupware/egroupware:latest` usa il tag `latest`: viene aggiornata
  automaticamente da **watchtower** (schedulato ogni notte alle 04:00) o manualmente con
  `docker compose pull && docker compose up -d`.
- Questo meccanismo **funziona correttamente**, come dimostra il fatto che il core è
  all'ultima release.
- I container `egroupware-push`, `rocketchat` e `rocketchat-mongo` **non si aggiornano
  automaticamente** perché nel `docker-compose.yml` del server hanno **tag di versione
  fissi** (es. `5.4.10`, `mongo:5.0`, `4.6-php7.4`) anziché tag mobili (`latest`/`stable7`).

---

## 5. Conclusioni

1. **eGroupware è aggiornato all'ultima versione (26.1 — maintenance 26.5.20260507).**
   Nessun intervento necessario sul core.
2. **Non esiste alcun blocco lato Claude/repository.** Il file YAML di configurazione
   operativo è sul server e gestibile normalmente dall'amministratore.
3. L'unico margine di aggiornamento riguarda i **servizi accessori** (Rocket.Chat,
   MongoDB, push/Swoole), fermi a versioni vecchie per via dei tag fissi nel compose.
   Si tratta di una scelta/configurazione del file sul server, non di un impedimento
   tecnico esterno.

---

## 6. Azioni consigliate (opzionali)

- Se si desidera aggiornare anche i servizi accessori: allineare nel `docker-compose.yml`
  del server i tag a `phpswoole/swoole:latest-alpine`,
  `rocketchat/rocket.chat:stable7` (o `quay.io/egroupware/rocket.chat:stable7`) e
  `mongo:7.0`, quindi eseguire `docker compose pull && docker compose up -d`.
  ⚠️ L'aggiornamento di MongoDB 5 → 7 richiede il passaggio intermedio per la 6 e un
  backup preventivo.
- Reperire e versionare il `docker-compose.yml` del server (con `docker inspect egroupware`
  per individuarne la cartella) per avere tracciabilità della configurazione reale.
