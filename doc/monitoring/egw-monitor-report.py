#!/usr/bin/env python3
"""
EGroupware performance monitoring: report

Analyses the logs written by the monitoring kit (see README.md):
- PHP-FPM access log: duration, CPU and memory of every request
- PHP-FPM slowlog: PHP backtraces of requests running longer than 3s
- MariaDB slow query log
- samples.jsonl: one sample per minute of host, FPM pool, MariaDB and containers

Only uses the Python standard library and runs with lowest priority, so it can
be run on the production host:

  egw-monitor-report.py --days 1
  egw-monitor-report.py --from "2026-10-12 08:00" --to "2026-10-12 12:00"
"""

import argparse
import glob
import gzip
import json
import os
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs, unquote

LOCAL_TZ = datetime.now().astimezone().tzinfo
# ActiveSync Ping is a long poll waiting for changes: long by design
LONG_POLL = re.compile(r'ActiveSync .*Cmd=Ping', re.I)


def open_logs(pattern):
	"""Yield lines of all (also rotated and gzipped) log files matching pattern, oldest first."""
	files = sorted(glob.glob(pattern + '*'), key=os.path.getmtime)
	for name in files:
		opener = gzip.open if name.endswith('.gz') else open
		try:
			with opener(name, 'rt', encoding='utf-8', errors='replace') as f:
				yield from f
		except OSError as e:
			print(f'Impossibile leggere {name}: {e}', file=sys.stderr)


def percentile(values, p):
	if not values:
		return 0
	values = sorted(values)
	return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def fmt_s(ms):
	return f'{ms / 1000:.1f}'


def table(rows, headers, aligns=None):
	"""Print a plain text table."""
	rows = [[str(c) for c in r] for r in rows]
	widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
	aligns = aligns or ['<'] + ['>'] * (len(headers) - 1)
	line = '  '.join(f'{{:{a}{w}}}' for a, w in zip(aligns, widths))
	print(line.format(*headers))
	print('  '.join('-' * w for w in widths))
	for r in rows:
		print(line.format(*r).rstrip())
	print()


def section(title):
	print()
	print('=' * len(title))
	print(title)
	print('=' * len(title))


def endpoint(method, uri):
	"""Group a request URI into an endpoint: script plus menuaction or DAV/ActiveSync command."""
	try:
		parts = urlsplit(uri)
	except ValueError:
		return uri[:80]
	path = unquote(parts.path)
	query = parse_qs(parts.query)
	path = re.sub(r'^/egroupware/', '', path)
	if 'menuaction' in query:
		return f"{path} {query['menuaction'][0]}"
	if 'Microsoft-Server-ActiveSync' in path:
		return f"ActiveSync Cmd={query.get('Cmd', ['?'])[0]}"
	m = re.match(r'(groupdav\.php|webdav\.php)(/.*)?$', path)
	if m:
		segments = [s for s in (m.group(2) or '').split('/') if s]
		# groupdav.php/<user>/<collection>/..., webdav.php/<dir>/...
		kind = segments[1] if m.group(1) == 'groupdav.php' and len(segments) > 1 else (segments[0] if segments else '')
		return f'{m.group(1)} {method} {re.sub(r"[0-9]+", "N", kind)}'
	return re.sub(r'[0-9]+', 'N', path) or '/'


# classification of what a slow request was waiting for, from its backtrace
NET_FUNCS = {'fread', 'fgets', 'fwrite', 'fputs', 'stream_select', 'stream_socket_client', 'fsockopen',
	'stream_socket_enable_crypto', 'feof', 'stream_get_line', 'stream_get_contents', 'socket_read', 'socket_write'}
FILE_FUNCS = {'fopen', 'file_get_contents', 'file_put_contents', 'rename', 'unlink', 'filesize', 'file_exists',
	'is_file', 'is_dir', 'scandir', 'opendir', 'readdir', 'copy', 'stat', 'filemtime', 'mkdir', 'rmdir', 'flock',
	'fclose', 'readfile', 'touch', 'is_readable', 'is_writable', 'glob', 'realpath', 'fflush', 'move_uploaded_file'}


def classify(frames):
	"""Return the category of the wait from a list of (function, file:line), innermost first."""
	if not frames:
		return 'sconosciuto'
	func = frames[0][0].split('::')[-1].split('->')[-1].lower()
	files = ' '.join(f for _, f in frames).lower()
	funcs = ' '.join(f for f, _ in frames).lower()
	if func in ('session_start', 'session_write_close') or (func == 'flock' and 'session' in files):
		return 'lock sessione PHP'
	if 'mysqli' in func or 'mysqli' in funcs[:200] or func.startswith('pdo') or 'pdostatement' in funcs[:200] \
			or func in ('mysql_query', 'query') and 'adodb' in files:
		return 'database'
	if func.startswith('ldap_'):
		return 'LDAP'
	if func.startswith('curl_'):
		return 'HTTP esterno (curl)'
	if func in NET_FUNCS:
		if 'imap' in files:
			return 'IMAP'
		if 'smtp' in files or 'mail/transport' in files:
			return 'SMTP'
		if 'sieve' in files:
			return 'Sieve'
		if 'ldap' in files:
			return 'LDAP'
		if 'vfs' in files or 'sqlfs' in files or 'filemanager' in files:
			return 'filesystem/VFS'
		return 'rete/socket'
	if func in FILE_FUNCS:
		return 'filesystem/VFS'
	if func in ('usleep', 'sleep', 'time_nanosleep'):
		return 'sleep'
	return 'CPU/PHP'


def short_path(path):
	return re.sub(r'^.*?/egroupware/', '', path)


def parse_args():
	ap = argparse.ArgumentParser(description='Report delle prestazioni di EGroupware')
	ap.add_argument('--dir', default=os.environ.get('EGW_MONITOR_LOG_DIR', '/var/log/egroupware-monitor'),
		help='directory dei log (default %(default)s)')
	ap.add_argument('--days', type=float, default=1, help='analizza gli ultimi N giorni (default 1)')
	ap.add_argument('--from', dest='date_from', help='inizio, ora locale "YYYY-MM-DD HH:MM"')
	ap.add_argument('--to', dest='date_to', help='fine, ora locale "YYYY-MM-DD HH:MM"')
	ap.add_argument('--top', type=int, default=25, help='righe per tabella (default 25)')
	ap.add_argument('--slow-ms', type=int, default=2000, help='soglia richiesta lenta in ms (default 2000)')
	args = ap.parse_args()
	now = datetime.now(timezone.utc)
	args.start = now - timedelta(days=args.days)
	args.end = now
	for attr, value in (('start', args.date_from), ('end', args.date_to)):
		if value:
			setattr(args, attr, datetime.fromisoformat(value).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc))
	return args


def read_access(args):
	"""Parse the PHP-FPM access log: list of dicts."""
	requests = []
	for line in open_logs(os.path.join(args.dir, 'fpm', 'fpm-access.log')):
		parts = line.rstrip('\n').split(' ', 7)
		if len(parts) < 8:
			continue
		try:
			ts = datetime.strptime(parts[0], '%Y-%m-%dT%H:%M:%S%z')
			if not args.start <= ts <= args.end:
				continue
			requests.append({
				'ts': ts, 'pid': parts[1], 'method': parts[2], 'status': parts[3],
				'ms': float(parts[4]), 'mem_mb': float(parts[5]) / 1024, 'cpu': float(parts[6].rstrip('%')),
				'uri': parts[7], 'endpoint': endpoint(parts[2], parts[7]),
			})
		except ValueError:
			continue
	return requests


def read_slowlog(args):
	"""Parse the PHP-FPM slowlog: list of dicts with time, pid, script and frames (innermost first)."""
	entries, entry = [], None
	head = re.compile(r'^\[(\d\d-\w{3}-\d{4} \d\d:\d\d:\d\d)\]\s+\[pool (\S+)\] pid (\d+)')
	frame = re.compile(r'^\[0x[0-9a-f]+\] (\S+)\(\) (.*)$')
	for line in open_logs(os.path.join(args.dir, 'fpm', 'fpm-slow.log')):
		m = head.match(line)
		if m:
			# FPM writes the slowlog in the container's time zone, which is UTC in the EGroupware image
			ts = datetime.strptime(m.group(1), '%d-%b-%Y %H:%M:%S').replace(tzinfo=timezone.utc)
			entry = {'ts': ts, 'pid': m.group(3), 'script': '', 'frames': []} if args.start <= ts <= args.end else None
			if entry:
				entries.append(entry)
			continue
		if not entry:
			continue
		if line.startswith('script_filename = '):
			entry['script'] = short_path(line.split('=', 1)[1].strip())
		else:
			m = frame.match(line.strip())
			if m:
				entry['frames'].append((m.group(1), short_path(m.group(2))))
	return entries


def read_mysql_slow(args):
	"""Parse the MariaDB slow query log: list of dicts."""
	queries, cur, sql = [], None, []

	def finish():
		if cur and sql:
			cur['sql'] = ' '.join(sql)
			if args.start <= cur['ts'] <= args.end:
				queries.append(cur)

	for line in open_logs(os.path.join(args.dir, 'mariadb', 'mariadb-slow.log')):
		if line.startswith('# Time:') or (line.startswith('# User@Host') and sql):
			finish()
			cur, sql = None, []
		if line.startswith('# Query_time:'):
			finish()
			m = re.search(r'Query_time:\s*([\d.]+)\s+Lock_time:\s*([\d.]+)\s+Rows_sent:\s*(\d+)\s+Rows_examined:\s*(\d+)', line)
			cur, sql = ({'time': float(m.group(1)), 'lock': float(m.group(2)), 'rows_sent': int(m.group(3)),
				'rows_examined': int(m.group(4)), 'ts': args.end} if m else None), []
		elif cur and line.startswith('SET timestamp='):
			cur['ts'] = datetime.fromtimestamp(int(re.sub(r'\D', '', line)), timezone.utc)
		elif cur and not line.startswith('#') and not line.startswith('use ') and line.strip():
			sql.append(line.strip())
	finish()
	return queries


def normalize_sql(sql):
	sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.S)
	sql = re.sub(r"'(?:[^'\\]|\\.)*'", '?', sql)
	sql = re.sub(r'"(?:[^"\\]|\\.)*"', '?', sql)
	sql = re.sub(r'\b\d+(\.\d+)?\b', 'N', sql)
	sql = re.sub(r'\(\s*(?:[?N]\s*,\s*)+[?N]\s*\)', '(...)', sql)
	sql = re.sub(r'\s+', ' ', sql).strip().rstrip(';')
	return sql[:220]


def read_samples(args):
	samples = []
	for line in open_logs(os.path.join(args.dir, 'samples.jsonl')):
		try:
			s = json.loads(line)
			s['ts'] = datetime.strptime(s['ts'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
		except (ValueError, KeyError):
			continue
		if args.start - timedelta(minutes=5) <= s['ts'] <= args.end:
			samples.append(s)
	samples.sort(key=lambda s: s['ts'])
	# per-interval values from the cumulative counters of the previous sample
	for prev, s in zip(samples, samples[1:]):
		total = sum(s['cpu'].values()) - sum(prev['cpu'].values())
		if total > 0:
			s['iowait_pct'] = 100 * (s['cpu']['iowait'] - prev['cpu']['iowait']) / total
			s['steal_pct'] = 100 * (s['cpu'].get('steal', 0) - prev['cpu'].get('steal', 0)) / total
			s['busy_pct'] = 100 - 100 * (s['cpu']['idle'] - prev['cpu']['idle'] + s['cpu']['iowait'] - prev['cpu']['iowait']) / total
		if s.get('fpm') and prev.get('fpm') and s['fpm'].get('start time') == prev['fpm'].get('start time'):
			s['fpm_children_reached'] = s['fpm']['max children reached'] - prev['fpm']['max children reached']
		if s.get('db') and prev.get('db'):
			for key in ('Slow_queries', 'Innodb_row_lock_waits', 'Created_tmp_disk_tables', 'Questions'):
				if key in s['db'] and key in prev['db'] and s['db'][key] >= prev['db'][key]:
					s['d_' + key] = s['db'][key] - prev['db'][key]
	return [s for s in samples if s['ts'] >= args.start]


def report_requests(args, requests):
	section('1. Richieste PHP (PHP-FPM access log)')
	normal = [r for r in requests if not LONG_POLL.search(r['endpoint'])]
	if not requests:
		print('Nessuna richiesta nel periodo: il log fpm-access.log e\' attivo?')
		return
	slow = [r for r in normal if r['ms'] >= args.slow_ms]
	durations = [r['ms'] for r in normal]
	print(f'Richieste: {len(requests)} (escluse {len(requests) - len(normal)} ActiveSync Ping, lunghe per progetto)')
	print(f'Durata: mediana {fmt_s(percentile(durations, 50))} s, 95° percentile {fmt_s(percentile(durations, 95))} s, '
		f'99° percentile {fmt_s(percentile(durations, 99))} s')
	if normal:
		print(f'Lente (>= {args.slow_ms / 1000:g} s): {len(slow)} = {100 * len(slow) / len(normal):.1f}%')
	if slow:
		io = [r for r in slow if r['cpu'] < 30]
		print(f'  di cui con CPU < 30%, cioe\' in attesa di qualcos\'altro (DB, IMAP, file, lock): '
			f'{len(io)} = {100 * len(io) / len(slow):.0f}%')
	print()

	by_ep = defaultdict(list)
	for r in normal:
		by_ep[r['endpoint']].append(r)
	rows = []
	for ep, rs in sorted(by_ep.items(), key=lambda kv: -sum(r['ms'] for r in kv[1]))[:args.top]:
		ms = [r['ms'] for r in rs]
		n_slow = sum(1 for m in ms if m >= args.slow_ms)
		rows.append([ep[:90], len(rs), fmt_s(sum(ms)), fmt_s(percentile(ms, 50)), fmt_s(percentile(ms, 95)),
			fmt_s(max(ms)), n_slow, f"{sum(r['cpu'] for r in rs) / len(rs):.0f}", f"{max(r['mem_mb'] for r in rs):.0f}"])
	print(f'Endpoint ordinati per tempo totale (top {args.top}); CPU% bassa con tempi alti = attesa esterna')
	table(rows, ['endpoint', 'n', 'tot s', 'p50 s', 'p95 s', 'max s', 'lente', 'CPU%', 'MB max'])

	rows = [[r['ts'].astimezone(LOCAL_TZ).strftime('%d/%m %H:%M:%S'), fmt_s(r['ms']), f"{r['cpu']:.0f}",
		f"{r['mem_mb']:.0f}", r['status'], r['endpoint'][:90]]
		for r in sorted(normal, key=lambda r: -r['ms'])[:args.top]]
	print(f'Le {args.top} richieste piu\' lente')
	table(rows, ['quando', 's', 'CPU%', 'MB', 'HTTP', 'endpoint'], ['<', '>', '>', '>', '>', '<'])


def report_slowlog(args, entries, requests):
	section('2. Dove aspettano le richieste lente (PHP-FPM slowlog, stack a 3 s)')
	if not entries:
		print('Nessuna traccia nel periodo (nessuna richiesta oltre 3 s, oppure manca la capability SYS_PTRACE:'
			' vedere "ptrace" nei log del container egroupware).')
		return
	# match each trace to its request: same worker pid, trace time inside the request
	by_pid = defaultdict(list)
	for r in requests:
		by_pid[r['pid']].append(r)
	for e in entries:
		e['endpoint'] = e['script']
		for r in by_pid.get(e['pid'], []):
			if r['ts'] <= e['ts'] <= r['ts'] + timedelta(milliseconds=r['ms'] + 1000):
				e['endpoint'] = r['endpoint']
				break
		e['category'] = classify(e['frames'])
	entries = [e for e in entries if not LONG_POLL.search(e['endpoint'])]
	if not entries:
		print('Solo tracce di ActiveSync Ping (attese per progetto).')
		return

	cats = Counter(e['category'] for e in entries)
	print(f'Tracce: {len(entries)}. Cosa stavano facendo dopo 3 s:')
	table([[c, n, f'{100 * n / len(entries):.0f}%'] for c, n in cats.most_common()], ['attesa', 'n', '%'])

	def egw_frame(frames):
		"""First frame in EGroupware code (not vendor), as this tells which feature is waiting."""
		for func, where in frames:
			if '/vendor/' not in where and not where.startswith('vendor/'):
				return f'{func}() {where}'
		return f'{frames[-1][0]}() {frames[-1][1]}' if frames else '?'

	combos = Counter((e['category'], e['frames'][0][0] if e['frames'] else '?', egw_frame(e['frames'])) for e in entries)
	print(f'Punti di attesa piu\' frequenti (top {args.top})')
	table([[cat, leaf, where[:100], n] for (cat, leaf, where), n in combos.most_common(args.top)],
		['attesa', 'funzione', 'codice EGroupware', 'n'], ['<', '<', '<', '>'])

	by_ep = defaultdict(Counter)
	for e in entries:
		by_ep[e['endpoint']][e['category']] += 1
	rows = [[ep[:90], sum(c.values()), ', '.join(f'{k} {v}' for k, v in c.most_common(3))]
		for ep, c in sorted(by_ep.items(), key=lambda kv: -sum(kv[1].values()))[:args.top]]
	print('Endpoint con piu\' tracce lente')
	table(rows, ['endpoint', 'tracce', 'attese principali'], ['<', '>', '<'])


def report_mysql(args, queries):
	section('3. Query lente MariaDB (slow query log, >= 1 s)')
	if not queries:
		print('Nessuna query lenta nel periodo (o slow log non attivo).')
		return
	groups = defaultdict(list)
	for q in queries:
		groups[normalize_sql(q['sql'])].append(q)
	print(f'Query lente: {len(queries)}, tempo totale {sum(q["time"] for q in queries):.0f} s')
	rows = [[len(qs), f"{sum(q['time'] for q in qs):.1f}", f"{max(q['time'] for q in qs):.1f}",
		f"{max(q['lock'] for q in qs):.1f}", max(q['rows_examined'] for q in qs), sql]
		for sql, qs in sorted(groups.items(), key=lambda kv: -sum(q['time'] for q in kv[1]))[:args.top]]
	table(rows, ['n', 'tot s', 'max s', 'lock s', 'righe esam.', 'query'], ['>', '>', '>', '>', '>', '<'])


def report_samples(args, samples, requests):
	section('4. Andamento orario del sistema (campioni al minuto)')
	if not samples:
		print('Nessun campione nel periodo: il cron di egw-monitor-sample.sh e\' attivo?')
		return
	hours = defaultdict(lambda: {'samples': [], 'requests': []})
	for s in samples:
		hours[s['ts'].astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:00')]['samples'].append(s)
	for r in requests:
		if not LONG_POLL.search(r['endpoint']):
			hours[r['ts'].astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:00')]['requests'].append(r)

	def avg(values):
		values = [v for v in values if v is not None]
		return sum(values) / len(values) if values else None

	def mx(values):
		values = [v for v in values if v is not None]
		return max(values) if values else None

	def f(v, fmt='{:.0f}'):
		return '-' if v is None else fmt.format(v)

	rows = []
	for hour in sorted(hours):
		ss, rs = hours[hour]['samples'], hours[hour]['requests']
		fpm = [s['fpm'] for s in ss if s.get('fpm')]
		db = [s['db'] for s in ss if s.get('db')]
		ms = [r['ms'] for r in rs]
		rows.append([f'{hour[8:10]}/{hour[5:7]} {hour[11:]}', len(rs), sum(1 for m in ms if m >= args.slow_ms), fmt_s(percentile(ms, 95)) if ms else '-',
			f(mx([p['active processes'] for p in fpm])), f(mx([p['total processes'] for p in fpm])),
			f(mx([p['listen queue'] for p in fpm])), f(sum(s.get('fpm_children_reached', 0) for s in ss)),
			f(avg([s.get('busy_pct') for s in ss])), f(mx([s.get('iowait_pct') for s in ss])),
			f(mx([s.get('steal_pct') for s in ss])), f(mx([s['load'][0] for s in ss]), '{:.1f}'),
			f(mx([d.get('Threads_running') for d in db])), f(mx([d.get('Queries_running_5s') for d in db])),
			f(sum(s.get('d_Innodb_row_lock_waits', 0) for s in ss))])
	print('FPM att/tot = worker PHP occupati/avviati (massimo); coda = richieste in attesa di un worker;')
	print('limite = volte in cui e\' stato raggiunto pm.max_children; CPU/iowait/steal in % dell\'host;')
	print('DB run = thread MariaDB in esecuzione; DB >5s = query in corso da oltre 5 s; lock = attese lock InnoDB')
	table(rows, ['ora', 'rich.', 'lente', 'p95 s', 'FPM att', 'FPM tot', 'coda', 'limite', 'CPU%', 'iowait%',
		'steal%', 'load', 'DB run', 'DB >5s', 'lock'])

	usage = defaultdict(list)
	for s in samples:
		for name, c in (s.get('containers') or {}).items():
			usage[name].append(c.get('cpu', 0))
	if usage:
		print('CPU dei container (% di un core: 200 = due core pieni)')
		table([[n, f'{sum(v) / len(v):.0f}', f'{percentile(v, 95):.0f}', f'{max(v):.0f}']
			for n, v in sorted(usage.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))],
			['container', 'media', 'p95', 'max'])

	probes = defaultdict(list)
	for s in samples:
		for url, t in (s.get('probes') or {}).items():
			probes[url].append(t)
	if probes:
		print('Connessioni di prova (TCP + TLS, secondi)')
		table([[url, sum(1 for t in ts if t is None), f'{percentile([t[1] or t[0] for t in ts if t], 50):.3f}',
			f'{max((t[1] or t[0] for t in ts if t), default=0):.3f}'] for url, ts in probes.items()],
			['url', 'fallite', 'mediana', 'max'])


def report_hints(args, requests, entries, queries, samples):
	section('5. Indicazioni automatiche')
	hints = []
	normal = [r for r in requests if not LONG_POLL.search(r['endpoint'])]
	slow = [r for r in normal if r['ms'] >= args.slow_ms]
	if slow and sum(1 for r in slow if r['cpu'] < 30) > len(slow) / 2:
		hints.append('La maggior parte delle richieste lente usa poca CPU: il collo di bottiglia e\' un\'attesa '
			'esterna (vedi sezione 2), non la potenza di calcolo.')
	elif slow:
		hints.append('Molte richieste lente usano molta CPU: elaborazione PHP pesante (liste grandi, rendering,'
			' allegati); valutare CPU del server e opcache.')
	cats = Counter(e.get('category') for e in entries if not LONG_POLL.search(e.get('endpoint', '')))
	for cat, text in (
			('lock sessione PHP', 'Richieste ferme sul lock della sessione PHP: richieste parallele dello stesso utente'
				' si mettono in fila dietro una lenta (tipicamente mail o liste).'),
			('IMAP', 'Attese sul server IMAP: verificare latenza/carico del server di posta e dimensione delle cartelle.'),
			('database', 'Attese sul database: incrociare con le query lente (sezione 3) e i lock InnoDB.'),
			('filesystem/VFS', 'Attese sul filesystem: verificare il disco dei dati (iowait) o eventuali mount di rete.'),
			('LDAP', 'Attese su LDAP/Active Directory: verificare il server di autenticazione.'),
			('HTTP esterno (curl)', 'Attese su chiamate HTTP esterne (Collabora, Rocket.Chat, push, servizi esterni).')):
		if cats.get(cat, 0) >= max(3, 0.1 * sum(cats.values())):
			hints.append(f'{text} ({cats[cat]} tracce)')
	if any((s.get('fpm') or {}).get('listen queue', 0) > 0 or s.get('fpm_children_reached', 0) > 0 for s in samples):
		hints.append('Tutti i worker PHP-FPM sono stati occupati in alcuni minuti (coda/limite > 0): le richieste'
			' aspettano prima ancora di partire. Cercare la causa delle richieste lunghe prima di alzare pm.max_children.')
	if any((s.get('iowait_pct') or 0) > 20 for s in samples):
		hints.append('iowait oltre il 20% in alcuni minuti: il disco e\' saturo (backup, indicizzazioni, altri container?).')
	if any((s.get('steal_pct') or 0) > 10 for s in samples):
		hints.append('steal oltre il 10%: la macchina virtuale aspetta CPU dall\'hypervisor, il problema e\' sull\'host fisico.')
	if any((s.get('mem_kb') or {}).get('swap_used', 0) > 256 * 1024 for s in samples):
		hints.append('Uso di swap oltre 256 MB: memoria insufficiente, verificare i container piu\' grandi.')
	if queries:
		hints.append(f'{len(queries)} query oltre 1 s: vedi sezione 3 per tabelle/indici da verificare.')
	for h in hints or ['Nessuna anomalia evidente nel periodo analizzato.']:
		print('- ' + h)
	print()


def main():
	args = parse_args()
	try:
		os.nice(19)
	except OSError:
		pass
	requests = read_access(args)
	entries = read_slowlog(args)
	queries = read_mysql_slow(args)
	samples = read_samples(args)
	print(f"Report prestazioni EGroupware dal {args.start.astimezone(LOCAL_TZ):%d/%m/%Y %H:%M} "
		f"al {args.end.astimezone(LOCAL_TZ):%d/%m/%Y %H:%M} (ora locale), log in {args.dir}")
	report_requests(args, requests)
	report_slowlog(args, entries, requests)
	report_mysql(args, queries)
	report_samples(args, samples, requests)
	report_hints(args, requests, entries, queries, samples)


if __name__ == '__main__':
	main()
