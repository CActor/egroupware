#!/bin/bash
################################################################################
##
## EGroupware performance monitoring: take one sample of the system state
##
## Run once a minute by cron on the Docker host (see install.sh), appends one
## line of JSON to $EGW_MONITOR_LOG_DIR/samples.jsonl with:
## - host: load, CPU/iowait counters from /proc/stat, available memory
## - PHP-FPM pool status: busy/idle workers, listen queue, max children reached
## - MariaDB: running/connected threads, long running queries, counters
## - CPU and memory of every container (docker stats)
## - optional timing of TCP+TLS handshake to e.g. the IMAP server
##
## Runs with lowest CPU and IO priority and never twice at the same time.
##
################################################################################

LOG_DIR=${EGW_MONITOR_LOG_DIR:-/var/log/egroupware-monitor}
EGW_CONTAINER=${EGW_CONTAINER:-egroupware}
DB_CONTAINER=${DB_CONTAINER:-egroupware-db}
# space separated URLs to time, e.g. "imaps://mail.example.org:993 https://www.example.org/"
PROBE_URLS=${EGW_MONITOR_PROBE_URLS:-}
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

# lowest priority, never overlapping
renice -n 19 -p $$ >/dev/null 2>&1
ionice -c 3 -p $$ >/dev/null 2>&1
exec 9>"$LOG_DIR/.sample.lock"
flock -n 9 || exit 0

# escape a string for JSON
json_str() {
	local s=${1//\\/\\\\}
	s=${s//\"/\\\"}
	printf '"%s"' "${s//$'\t'/ }"
}

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
read -r load1 load5 load15 _ < /proc/loadavg
# cpu  user nice system idle iowait irq softirq steal
read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ < /proc/stat
mem_total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
mem_avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
swap_used=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print t-f}' /proc/meminfo)

# PHP-FPM status via FastCGI from inside the container (not published via webserver)
fpm=$(timeout 10 docker exec -i "$EGW_CONTAINER" php < "$SCRIPT_DIR/fpm-status.php" 2>/dev/null)
[ -n "$fpm" ] || fpm=null

# MariaDB status, using the root password from the db container's environment
db=null
if docker inspect "$DB_CONTAINER" >/dev/null 2>&1
then
	db=$(timeout 10 docker exec -i "$DB_CONTAINER" sh -c 'MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" \
		$(command -v mariadb || command -v mysql) -uroot -N -B' 2>/dev/null <<-EOSQL |
		SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_running', 'Threads_connected', 'Questions',
			'Slow_queries', 'Innodb_row_lock_waits', 'Innodb_row_lock_time', 'Innodb_buffer_pool_reads',
			'Innodb_buffer_pool_read_requests', 'Created_tmp_disk_tables');
		SELECT 'Queries_running_5s', COUNT(*) FROM information_schema.PROCESSLIST WHERE COMMAND='Query' AND TIME >= 5;
		EOSQL
		awk 'BEGIN {ORS=""; print "{"} NR>1 {print ","} {printf "\"%s\":%s", $1, $2+0} END {print "}"}')
	{ [ "$db" = "{}" ] || [ -z "$db" ]; } && db=null
fi

# CPU and memory of all containers
containers=$(timeout 20 docker stats --no-stream --format '{{.Name}}	{{.CPUPerc}}	{{.MemUsage}}' 2>/dev/null |
	awk -F'\t' 'BEGIN {ORS=""; print "{"} NR>1 {print ","}
		{cpu=$2; sub(/%/, "", cpu); split($3, m, " / "); printf "\"%s\":{\"cpu\":%s,\"mem\":\"%s\"}", $1, cpu+0, m[1]}
		END {print "}"}')
[ -n "$containers" ] || containers=null

# optional connect + TLS handshake times
probes="{"
for url in $PROBE_URLS
do
	t=$(curl -s -o /dev/null --max-time 10 -w '%{time_connect} %{time_appconnect}' "$url" 2>/dev/null)
	[ "$probes" = "{" ] || probes+=","
	# connect time 0 means the connection failed
	if [[ $t =~ ^[0-9.]+\ [0-9.]+$ && ! $t =~ ^0\.0+\  ]]
	then
		probes+="$(json_str "$url"):[${t% *},${t#* }]"
	else
		probes+="$(json_str "$url"):null"
	fi
done
probes+="}"

printf '{"ts":"%s","load":[%s,%s,%s],"cpu":{"user":%s,"nice":%s,"system":%s,"idle":%s,"iowait":%s,"irq":%s,"softirq":%s,"steal":%s},"mem_kb":{"total":%s,"available":%s,"swap_used":%s},"fpm":%s,"db":%s,"containers":%s,"probes":%s}\n' \
	"$ts" "$load1" "$load5" "$load15" \
	"$cpu_user" "$cpu_nice" "$cpu_system" "$cpu_idle" "$cpu_iowait" "$cpu_irq" "$cpu_softirq" "${cpu_steal:-0}" \
	"$mem_total" "$mem_avail" "$swap_used" \
	"$fpm" "$db" "$containers" "$probes" >> "$LOG_DIR/samples.jsonl"
