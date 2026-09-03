const BASE = '/api';

async function get(path, params) {
	const url = new URL(BASE + path, location.origin);
	for (const [key, value] of Object.entries(params ?? {})) {
		if (value !== undefined && value !== null && value !== '') {
			url.searchParams.set(key, value);
		}
	}
	const response = await fetch(url);
	if (!response.ok) {
		throw await failure(response);
	}
	return response.json();
}

// FastAPI puts the human-readable reason in `detail`; show that, not the JSON.
// A rejected search says what is wrong with it, so this is the text to surface.
async function failure(response) {
	const text = await response.text();
	let message = text;
	try {
		message = JSON.parse(text).detail ?? text;
	} catch {
		// not JSON - use the raw body
	}
	return new Error(message || `HTTP ${response.status}`);
}

async function send(method, path, body) {
	const response = await fetch(BASE + path, {
		method,
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body ?? {})
	});
	if (!response.ok) {
		throw await failure(response);
	}
	return response.json();
}

const post = (path, body) => send('POST', path, body);

export const api = {
	stats: () => get('/stats'),
	dbStatus: () => get('/db'),
	pickDatabase: (create) => post('/db/pick', { create }),
	connectDatabase: (path, create) => post('/db/connect', { path, create }),
	threads: (params) => get('/threads', params),
	thread: (id) => get(`/threads/${id}`),
	messages: (id, params) => get(`/threads/${id}/messages`, params),
	search: (params) => get('/search', params),
	monthStats: (params) => get('/stats/months', params),
	people: () => get('/people'),
	createPerson: (body) => post('/people', body),
	updatePerson: (personId, body) => send('PATCH', `/people/${personId}`, body),
	deletePerson: (personId) => send('DELETE', `/people/${personId}`),
	linkIdentities: (personId, userIds) =>
		post('/people/link', { person_id: personId, user_ids: userIds }),
	pickFolder: () => post('/ingest/pick-folder'),
	pickDht: () => post('/ingest/pick-dht'),
	inspect: (path) => post('/ingest/inspect', { path }),
	history: () => get('/ingest/history')
};

export const mediaUrl = (sha256) => `${BASE}/media/${sha256}`;

/**
 * Run an ingest, yielding each server-sent progress event as it arrives.
 */
export async function* streamIngest(path) {
	const response = await fetch(`${BASE}/ingest/run`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ path })
	});
	if (!response.ok) {
		yield { event: 'error', message: `${response.status} ${await response.text()}` };
		return;
	}

	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });

		let split;
		while ((split = buffer.indexOf('\n\n')) !== -1) {
			const chunk = buffer.slice(0, split).trim();
			buffer = buffer.slice(split + 2);
			if (chunk.startsWith('data:')) {
				try {
					yield JSON.parse(chunk.slice(5).trim());
				} catch {
					// ignore malformed frames
				}
			}
		}
	}
}
