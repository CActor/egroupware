#!/bin/bash
################################################################################
##
## EGroupware performance monitoring: install / uninstall on the Docker host
##
## Usage (as root):
##   ./install.sh [--compose-dir /etc/egroupware-docker] [--probe "imaps://mail.example.org:993"] [--yes]
##   ./install.sh --uninstall [--compose-dir /etc/egroupware-docker] [--yes]
##
## Installs:
## - the kit in /opt/egroupware-monitor
## - logs in /var/log/egroupware-monitor (rotated daily, kept 30 days)
## - docker-compose.monitoring.yml next to docker-compose.yml, added to COMPOSE_FILE in .env
## - a cron job sampling the system once a minute (/etc/cron.d/egroupware-monitor)
##
## The egroupware and db containers have to be recreated once (some seconds of
## downtime), the script asks before doing so.
##
################################################################################

set -e

KIT_DIR=$(dirname "$(readlink -f "$0")")
MONITOR_DIR=/opt/egroupware-monitor
LOG_DIR=/var/log/egroupware-monitor
COMPOSE_DIR=
PROBES=
YES=
UNINSTALL=

while [ $# -gt 0 ]
do
	case "$1" in
		--compose-dir) COMPOSE_DIR=$2; shift;;
		--probe) PROBES=$2; shift;;
		--yes|-y) YES=1;;
		--uninstall) UNINSTALL=1;;
		*) echo "Usage: $0 [--compose-dir DIR] [--probe 'URL ...'] [--yes] [--uninstall]"; exit 1;;
	esac
	shift
done

[ "$(id -u)" = 0 ] || { echo "Please run as root"; exit 1; }

# find the directory with EGroupware's docker-compose.yml
if [ -z "$COMPOSE_DIR" ]
then
	for dir in /etc/egroupware-docker "$PWD"
	do
		[ -f "$dir/docker-compose.yml" ] && { COMPOSE_DIR=$dir; break; }
	done
fi
[ -n "$COMPOSE_DIR" ] && [ -f "$COMPOSE_DIR/docker-compose.yml" ] || {
	echo "docker-compose.yml not found, use --compose-dir DIR"; exit 1;
}
cd "$COMPOSE_DIR"

if docker compose version >/dev/null 2>&1
then
	COMPOSE="docker compose"
else
	COMPOSE=docker-compose
fi

confirm() {
	[ -n "$YES" ] && return 0
	read -r -p "$1 [y/N] " answer
	[[ $answer =~ ^[yYsS] ]]
}

# set COMPOSE_FILE in .env to the given value, or remove it if empty
set_compose_file() {
	touch .env
	cp -p .env ".env.bak-egw-monitor-$(date +%Y%m%d%H%M%S)"
	sed -i '/^COMPOSE_FILE=/d' .env
	[ -z "$1" ] || echo "COMPOSE_FILE=$1" >> .env
}

if [ -n "$UNINSTALL" ]
then
	files=$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | tr ':' '\n' | grep -vx docker-compose.monitoring.yml | paste -sd: -)
	# only default files left: no need for COMPOSE_FILE anymore
	[ "$files" = "docker-compose.yml" ] || [ "$files" = "docker-compose.yml:docker-compose.override.yml" ] && files=
	set_compose_file "$files"
	rm -f docker-compose.monitoring.yml /etc/cron.d/egroupware-monitor /etc/logrotate.d/egroupware-monitor
	confirm "Recreate egroupware and db containers now (some seconds of downtime)?" && $COMPOSE up -d
	echo "Monitoring removed, logs kept in $LOG_DIR and the kit in $MONITOR_DIR (remove manually if no longer needed)."
	exit 0
fi

# PHP version inside the EGroupware container determines the FPM config directory
PHP_VERSION=$(docker exec egroupware sh -c 'echo $PHP_VERSION' 2>/dev/null) || true
[ -n "$PHP_VERSION" ] && docker exec egroupware test -d "/etc/php/$PHP_VERSION/fpm/pool.d" || {
	echo "Can not determine PHP version in container egroupware, is it running?"; exit 1;
}
[[ $PHP_VERSION =~ ^[89]\. ]] || { echo "PHP $PHP_VERSION is too old, PHP-FPM >= 8.0 needed"; exit 1; }

HAS_DB=
$COMPOSE config --services 2>/dev/null | grep -qx db && docker inspect egroupware-db >/dev/null 2>&1 && HAS_DB=1

echo "Installing EGroupware monitoring: compose dir $COMPOSE_DIR, PHP $PHP_VERSION, MariaDB container: ${HAS_DB:-no}"

# kit and log directories
mkdir -p "$MONITOR_DIR/fpm" "$MONITOR_DIR/mariadb" "$LOG_DIR/fpm" "$LOG_DIR/mariadb"
install -m 644 "$KIT_DIR/fpm/zz-egw-monitor.conf" "$MONITOR_DIR/fpm/"
install -m 644 "$KIT_DIR/mariadb/egw-monitor.cnf" "$MONITOR_DIR/mariadb/"
install -m 644 "$KIT_DIR/fpm-status.php" "$KIT_DIR/README.md" "$MONITOR_DIR/"
install -m 755 "$KIT_DIR/egw-monitor-sample.sh" "$KIT_DIR/egw-monitor-report.py" "$MONITOR_DIR/"
# logs contain URLs of the users' requests: root only
chmod 750 "$LOG_DIR" "$LOG_DIR/fpm"
if [ -n "$HAS_DB" ]
then
	# MariaDB writes its slow log as user mysql of the container
	DB_UID=$(docker exec egroupware-db id -u mysql 2>/dev/null || echo 999)
	chown "$DB_UID" "$LOG_DIR/mariadb"
	chmod 750 "$LOG_DIR/mariadb"
fi

# compose file, without the db part if the database is not our db container
sed -e "s|@PHP_VERSION@|$PHP_VERSION|g" -e "s|@MONITOR_DIR@|$MONITOR_DIR|g" \
	"$KIT_DIR/docker-compose.monitoring.yml" > docker-compose.monitoring.yml.new
[ -n "$HAS_DB" ] || sed -i '/^  db:/,$d' docker-compose.monitoring.yml.new
mv docker-compose.monitoring.yml.new docker-compose.monitoring.yml

files=$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null)
if [ -z "$files" ]
then
	files=docker-compose.yml
	[ -f docker-compose.override.yml ] && files+=:docker-compose.override.yml
fi
[[ ":$files:" == *:docker-compose.monitoring.yml:* ]] || files+=:docker-compose.monitoring.yml
set_compose_file "$files"
$COMPOSE config -q || { echo "Invalid compose configuration, check $COMPOSE_DIR/.env and docker-compose.monitoring.yml"; exit 1; }

# sampling once a minute and log rotation
cat > /etc/cron.d/egroupware-monitor <<EOF
# EGroupware performance monitoring: one sample per minute, see $MONITOR_DIR/README.md
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EGW_MONITOR_PROBE_URLS="$PROBES"
* * * * * root $MONITOR_DIR/egw-monitor-sample.sh >/dev/null 2>&1
EOF
install -m 644 "$KIT_DIR/logrotate/egroupware-monitor" /etc/logrotate.d/egroupware-monitor

if confirm "Recreate egroupware${HAS_DB:+ and db} container(s) now to activate the monitoring (some seconds of downtime)?"
then
	$COMPOSE up -d egroupware ${HAS_DB:+db}
	echo "Waiting for PHP-FPM ..."
	for _ in $(seq 30)
	do
		docker exec -i egroupware php < "$MONITOR_DIR/fpm-status.php" >/dev/null 2>&1 && break
		sleep 2
	done
	if docker exec -i egroupware php < "$MONITOR_DIR/fpm-status.php" >/dev/null 2>&1
	then
		echo "PHP-FPM status: OK"
	else
		echo "PHP-FPM status NOT available, check: docker logs egroupware"
	fi
	if [ -n "$HAS_DB" ]
	then
		docker exec egroupware-db sh -c 'MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}" \
			$(command -v mariadb || command -v mysql) -uroot -N -e "SELECT @@slow_query_log"' 2>/dev/null | grep -qx 1 &&
			echo "MariaDB slow query log: OK" || echo "MariaDB slow query log NOT active, check: docker logs egroupware-db"
	else
		echo "No MariaDB container: enable the slow query log on your database server, see $MONITOR_DIR/README.md"
	fi
	"$MONITOR_DIR/egw-monitor-sample.sh" && tail -1 "$LOG_DIR/samples.jsonl" | cut -c1-200
else
	echo "Run \"cd $COMPOSE_DIR && $COMPOSE up -d\" later to activate the monitoring."
fi
echo "Done. Report: $MONITOR_DIR/egw-monitor-report.py --days 1"
