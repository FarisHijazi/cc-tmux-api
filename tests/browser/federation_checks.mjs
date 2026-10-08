import assert from 'node:assert/strict';

// Explicit deployment targets only; importing this module makes no requests.
export function testOrigin(value = process.env.MOBILE_BASE_URL || 'http://100.115.53.98:8389') {
  const url = new URL(value);
  assert(['http://100.115.53.98:8389', 'http://127.0.0.1:8389', 'http://127.0.0.1:8391'].includes(url.origin), 'Unapproved browser target');
  assert(url.href === `${url.origin}/`, 'Target must be an origin without credentials, path, or query');
  return url.origin;
}

export function decodeRoute(id) {
  const hops = [];
  for (let depth = 0; depth < 8; depth++) {
    const [host, identity] = JSON.parse(Buffer.from(id, 'base64url').toString());
    assert.equal(typeof host, 'string');
    hops.push(host);
    if (Array.isArray(identity)) return { hops, identity, ownerId: id };
    assert.equal(typeof identity?.peer, 'string', 'Invalid peer identity');
    id = identity.peer;
  }
  throw new Error('Peer identity exceeds bounded route depth');
}

export async function auditFleet({ context, fleet, report, name }) {
  const origins = new Set();
  const details = [];
  for (const row of fleet.sessions) {
    assert(row.origin_id && !origins.has(row.origin_id), 'Missing or duplicate origin identity');
    origins.add(row.origin_id);
    const response = await context.request.get(`/api/sessions/${encodeURIComponent(row.id)}`);
    assert(response.ok(), `Detail HTTP ${response.status()}: ${row.session}`);
    const detail = await response.json();
    for (const field of ['id', 'origin_id', 'owner_node_id', 'owner_session_id', 'pane', 'session', 'workdir']) {
      assert.deepEqual(detail[field], row[field], `Detail identity changed: ${field}`);
    }
    assert.deepEqual(detail.host_path, row.host_path);
    const route = decodeRoute(row.id);
    assert.equal(route.ownerId, row.owner_session_id);
    assert.equal(route.identity[1], row.pane);
    assert(Array.isArray(detail.messages), 'Missing messages array');
    assert.equal(typeof detail.transcript_status, 'string', 'Missing typed transcript status');
    details.push({ session: row.session, pane: row.pane, hostPath: row.host_path, origin: row.origin_id,
      peerHops: route.hops.length - 1, messages: detail.messages.length, transcriptStatus: detail.transcript_status,
      lifecycle: detail.lifecycle, alive: detail.alive });
  }
  assert(details.length > 0, 'No actual sessions discovered');
  const minimum = Number(process.env.MOBILE_MIN_PEER_HOPS || 0);
  assert(details.some(d => d.peerHops >= minimum), `No session traversed ${minimum} peer hops`);
  if (process.env.MOBILE_COMPARE_ROOT) {
    const root = testOrigin(process.env.MOBILE_COMPARE_ROOT);
    const response = await context.request.get(`${root}/api/sessions`);
    assert(response.ok(), 'Comparison root unavailable');
    const rootFleet = await response.json();
    assert.deepEqual([...origins].sort(), rootFleet.sessions.map(s => s.origin_id).sort(), 'Relay/root origin sets differ');
    report.checks.push({ name: `${name}: relay preserves root origin set`, status: 'passed', root });
  }
  report.checks.push({ name: `${name}: every actual session detail and route`, status: 'passed', details,
    hosts: fleet.hosts.map(h => ({ id: h.id, available: h.available, hostPath: h.host_path })) });
}
