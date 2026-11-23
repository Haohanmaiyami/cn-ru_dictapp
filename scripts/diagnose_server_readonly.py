#!/usr/bin/env python3
"""Run ON THE SERVER: sudo python3 diagnose_server_readonly.py
Optional backend request IDs (use your actual container name):
    sudo python3 diagnose_server_readonly.py --backend-container YOUR_BACKEND_CONTAINER
Without that option, no Docker container logs are read or containers discovered.
Read-only diagnostics. Only health GET requests; no production configuration changes.
No .env, keys, bodies, query values, client IPs, process commands or raw logs printed.
"""
import argparse
import collections
import datetime
import json
import math
import os
import re
import subprocess
import time
import urllib.request
from uuid import UUID

UTC = datetime.timezone.utc
KNOWN_ROUTES = frozenset({'/api/health', '/api/search', '/api/ai/analyze',
                          '/api/ai/translate-ru-to-cn'})
KNOWN_BACKEND_ROUTES = KNOWN_ROUTES | frozenset({
    '/api/entry/{entry_id}', '/', '/search', '/entry/{entry_id}', 'unmatched'})
KNOWN_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD',
                            'OPTIONS', 'TRACE', 'CONNECT', 'OTHER'})
INCIDENT_START = '2026-11-23 00:17:00 UTC'
INCIDENT_END = '2026-11-23 00:23:00 UTC'
MAX_RECENT = 80


def command(args, *, combine_output=False):
    try:
        # Docker sends container stderr (including Uvicorn logs) to its own
        # stderr. Capture both privately; only sanitized records are returned.
        p = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT if combine_output else subprocess.PIPE,
                           text=True, errors='replace', timeout=15)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return -1, ''


def valid_number(value, low, high):
    return (type(value) in (int, float) and math.isfinite(value)
            and low <= value <= high)


def sanitize_access_record(record):
    """Return only allowlisted fields, or None. Never stringify arbitrary values."""
    if not isinstance(record, dict):
        return None
    request = record.get('request')
    if not isinstance(request, dict):
        return None
    uri = request.get('uri')
    if not isinstance(uri, str):
        return None
    route = uri.split('?', 1)[0]
    status = record.get('status')
    ts = record.get('ts')
    if route not in KNOWN_ROUTES or type(status) is not int or not 100 <= status <= 599:
        return None
    if not valid_number(ts, 1577836800, 4102444800):  # 2020-2100, UTC
        return None
    clean = {'utc': datetime.datetime.fromtimestamp(ts, UTC).isoformat(),
             'route': route, 'status': status}
    duration = record.get('duration')
    if valid_number(duration, 0, 86400):
        clean['duration_seconds'] = round(duration, 6)
    size = record.get('size')
    if type(size) is int and 0 <= size <= 1_000_000_000_000:
        clean['response_bytes'] = size
    return clean


def summarize_journal(journal, max_lines):
    counts = collections.Counter()
    groups = {}
    recent = []
    tls_error_count = 0
    parsed_access = 0
    lines = journal.splitlines()[-max_lines:]
    oversized = 0
    for line in lines:
        if len(line) > 1_000_000:
            oversized += 1
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        logger = record.get('logger')
        if isinstance(logger, str) and logger.startswith('tls') and record.get('level') in ('error', 'warn'):
            tls_error_count += 1
        if isinstance(record.get('request'), dict):
            parsed_access += 1
        clean = sanitize_access_record(record)
        if clean is None:
            continue
        route, status = clean['route'], clean['status']
        key = (route, status)
        counts[key] += 1
        groups.setdefault(key, []).append(clean)
        recent.append(clean)
    aggregates = []
    for (route, status), count in sorted(counts.items()):
        records = groups[(route, status)]
        durations = [r['duration_seconds'] for r in records if 'duration_seconds' in r]
        sizes = [r['response_bytes'] for r in records if 'response_bytes' in r]
        item = {'route': route, 'status': status, 'count': count,
                'duration_samples': len(durations), 'size_samples': len(sizes)}
        if durations:
            item.update(duration_min_seconds=min(durations), duration_max_seconds=max(durations),
                        duration_mean_seconds=round(sum(durations) / len(durations), 6))
        if sizes:
            item['response_bytes_sum'] = sum(sizes)
        aggregates.append(item)
    return {'journal_lines_examined': len(lines), 'oversized_lines_skipped': oversized,
            'parsed_access_records': parsed_access, 'known_api_counts': aggregates,
            'recent_known_api_requests': sorted(recent, key=lambda r: r['utc'])[-MAX_RECENT:],
            'tls_warning_error_count': tls_error_count}


def sanitize_backend_record(record):
    """Accept only our middleware event and its closed, typed diagnostic fields."""
    if not isinstance(record, dict) or record.get('event') != 'http_request':
        return None
    request_id = record.get('request_id')
    if not isinstance(request_id, str):
        return None
    try:
        parsed_id = UUID(request_id)
    except (ValueError, AttributeError):
        return None
    if parsed_id.version != 4 or str(parsed_id) != request_id:
        return None
    method, route = record.get('method'), record.get('route')
    status, duration = record.get('status'), record.get('duration_ms')
    if (not isinstance(method, str) or method not in KNOWN_METHODS
            or not isinstance(route, str) or route not in KNOWN_BACKEND_ROUTES
            or type(status) is not int or not 100 <= status <= 599
            or not valid_number(duration, 0, 86_400_000)):
        return None
    return {'request_id': request_id, 'method': method, 'route': route,
            'status': status, 'duration_ms': round(duration, 3)}


def summarize_backend_logs(logs, max_lines=3000):
    """Ignore Uvicorn prefixes and all arbitrary text, including exception logs."""
    lines = logs.splitlines()[-max_lines:]
    oversized, records = 0, []
    groups = collections.defaultdict(list)
    for line in lines:
        if len(line) > 1_000_000:
            oversized += 1
            continue
        start = line.find('{')
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except (ValueError, TypeError, RecursionError):
            continue
        clean = sanitize_backend_record(record)
        if clean is None:
            continue
        # --timestamps gives an RFC3339 UTC prefix. Never preserve other prefix
        # text or timestamps supplied inside an application log message.
        timestamp = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)\s', line)
        if timestamp:
            try:
                utc = datetime.datetime.fromisoformat(timestamp[1].replace('Z', '+00:00'))
                if 2020 <= utc.year < 2100:
                    clean['utc'] = utc.isoformat()
            except ValueError:
                pass
        records.append(clean)
        groups[(clean['method'], clean['route'], clean['status'])].append(clean['duration_ms'])
    aggregates = []
    for (method, route, status), durations in sorted(groups.items()):
        aggregates.append({'method': method, 'route': route, 'status': status,
                           'count': len(durations), 'duration_min_ms': min(durations),
                           'duration_max_ms': max(durations),
                           'duration_mean_ms': round(sum(durations) / len(durations), 3)})
    return {'backend_log_lines_examined': len(lines), 'oversized_lines_skipped': oversized,
            'request_diagnostics_records': len(records),
            'no_request_diagnostics_found': not records,
            'request_counts': aggregates, 'recent_requests': records[-MAX_RECENT:]}


def backend_container_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise argparse.ArgumentTypeError('Use one existing Docker container name or ID.')
    return value


def collect_backend_windows(container, now):
    windows = []
    iso = lambda value: value.astimezone(UTC).isoformat().replace('+00:00', 'Z')
    incident = lambda value: datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)
    for label, since, until in [
            ('reported_incident', iso(incident(INCIDENT_START)), iso(incident(INCIDENT_END))),
            ('last_30_minutes', iso(now - datetime.timedelta(minutes=30)), iso(now))]:
        rc, logs = command(['docker', 'logs', '--timestamps', '--since', since,
                            '--until', until, '--tail', '3000', container], combine_output=True)
        window = {'label': label, 'since': since, 'until': until,
                  'readable': rc == 0, 'maximum_log_records_requested': 3000}
        window.update(summarize_backend_logs(logs if rc == 0 else ''))
        windows.append(window)
    return windows


def probe(name, url):
    started = time.monotonic()
    result = {'name': name}
    try:
        # Probe the server directly, without shell/environment proxy routing.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as r:
            result['http_status'] = r.status
    except Exception as e:
        result['error_class'] = type(e).__name__
        code = getattr(e, 'code', None)
        result['http_status'] = code if type(code) is int and 100 <= code <= 599 else None
    result['elapsed_seconds'] = round(time.monotonic() - started, 6)
    return result


def resource_summary():
    result = {'cpu_count': os.cpu_count()}
    try:
        result['load_average_1_5_15_minutes'] = [round(x, 3) for x in os.getloadavg()]
    except OSError:
        result['load_average_1_5_15_minutes'] = None
    allowed = {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}
    try:
        with open('/proc/meminfo') as stream:
            memory = {}
            for line in stream:
                match = re.fullmatch(r'([A-Za-z]+):\s+(\d+)\s+kB\s*', line)
                if match and match[1] in allowed:
                    memory[match[1] + '_bytes'] = int(match[2]) * 1024
        result['memory'] = memory
    except OSError:
        result['memory'] = None
    result['disks'] = []
    for label, path in [('root_filesystem', '/'), ('docker_storage_filesystem', '/var/lib/docker')]:
        try:
            s = os.statvfs(path)
            result['disks'].append({'label': label, 'total_bytes': s.f_blocks * s.f_frsize,
                                   'available_bytes': s.f_bavail * s.f_frsize,
                                   'free_bytes': s.f_bfree * s.f_frsize})
        except OSError:
            result['disks'].append({'label': label, 'readable': False})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend-container', type=backend_container_name,
                        help='Read bounded logs from this explicitly named backend container; optional.')
    options = parser.parse_args(argv)
    now = datetime.datetime.now(UTC)
    report = {'utc': now.isoformat(), 'resources_now': resource_summary()}
    for service in ['caddy', 'docker']:
        rc, text = command(['systemctl', 'is-active', service])
        report[service] = text.strip() if text.strip() in ['active', 'inactive', 'failed', 'activating'] else 'unknown'
    report['probes'] = [probe('local_backend', 'http://127.0.0.1:8000/api/health'),
                        probe('public_https_from_server', 'https://31.129.45.131/api/health')]
    rc, listing = command(['systemctl', 'list-timers', '--all', '--no-pager'])
    report['certificate_timer_listing_readable'] = rc == 0
    report['certificate_related_timer_line_count'] = sum(
        1 for line in listing.splitlines() if any(word in line.lower() for word in ['certbot', 'acme', 'lego']))
    report['caddy_windows'] = []
    for label, since, until, limit in [
            ('reported_incident', INCIDENT_START, INCIDENT_END, 6000),
            ('last_30_minutes', '30 minutes ago', None, 3000)]:
        args = ['journalctl', '-u', 'caddy', '--since', since,
                '-n', str(limit), '--no-pager', '-o', 'cat']
        if until:
            args.extend(['--until', until])
        rc, journal = command(args)
        window = {'label': label, 'since': since, 'until': until,
                  'readable': rc == 0, 'maximum_journal_records_requested': limit}
        window.update(summarize_journal(journal, limit))
        report['caddy_windows'].append(window)
    report['backend_logs_requested'] = options.backend_container is not None
    report['backend_windows'] = (collect_backend_windows(options.backend_container, now)
                                 if options.backend_container else [])
    rc, kernel = command(['journalctl', '-k', '--since', INCIDENT_START, '--until', INCIDENT_END,
                          '-n', '3000', '--no-pager', '-o', 'cat'])
    report['kernel_oom_incident_window'] = {
        'since': INCIDENT_START, 'until': INCIDENT_END, 'readable': rc == 0,
        'maximum_records_requested': 3000,
        'matching_log_line_count': sum(1 for line in kernel.splitlines()
                                       if re.search(r'out of memory|oom-kill|oom_reaper|killed process', line, re.I))}
    report['caveats'] = [
        'All logs are bounded samples, not a complete request history. OOM count is matching log lines, not distinct incidents.',
        'No access records is inconclusive: logging may be disabled, written to files, rotated or unavailable.',
        'Backend request IDs require the diagnostics middleware to be deployed; absent records do not prove no requests arrived.',
        'Backend container logs are read only when --backend-container is provided; containers are never auto-discovered.',
        'Caddy duration covers reverse-proxy request handling; response bytes do not prove the phone received the response.',
        'Health GET timing is measured from this server now, not from a Russian mobile/VPN client during the incident.',
        'Health is liveness only: HTTP 200 does not establish database, dictionary search or AI-provider readiness.',
        'Resource usage is current only; disk labels may describe the same filesystem.',
        'The incident window assumes November 22 2026 19:19-19:20 America/New_York; correct it if the reported time differs.',
        'No query values, bodies, client IPs, process commands or raw journal entries are included.'
    ]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
