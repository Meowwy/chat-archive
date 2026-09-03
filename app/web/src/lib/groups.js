/**
 * Conversations, grouped the way the app talks about people.
 *
 * Somebody you talk to on both Discord and Instagram is one entry under the
 * name you gave them, carrying every chat with them; anything without a single
 * mapped counterpart - group chats, unmapped identities - stays as its own
 * row. The conversation list and the Stats list are two views of exactly this,
 * so they share it rather than each inventing their own idea of a person.
 */
export function groupThreads(threads) {
	const people = new Map();
	const entries = [];
	for (const thread of threads) {
		if (thread.person_id == null) {
			entries.push({
				key: `t${thread.id}`,
				person_id: null,
				name: thread.name,
				href: `/t/${thread.id}`,
				threads: [thread],
				messages: thread.messages,
				last_ts: thread.last_ts,
				newest: thread,
				group: thread.kind === 'GROUP' ? thread.participants.length : 0
			});
			continue;
		}
		let entry = people.get(thread.person_id);
		if (!entry) {
			entry = {
				key: `p${thread.person_id}`,
				person_id: thread.person_id,
				name: thread.person,
				href: `/t/${thread.id}`,
				threads: [],
				messages: 0,
				last_ts: 0,
				newest: thread,
				group: 0
			};
			people.set(thread.person_id, entry);
			entries.push(entry);
		}
		entry.threads.push(thread);
		entry.messages += thread.messages;
		if (thread.last_ts > entry.last_ts) {
			entry.last_ts = thread.last_ts;
			entry.newest = thread;
			entry.href = `/t/${thread.id}`;
		}
	}
	return entries.sort((a, b) => b.last_ts - a.last_ts);
}

/** Filter grouped entries by a typed name, matching the person or any chat. */
export function filterEntries(entries, query) {
	const needle = query.trim().toLowerCase();
	if (!needle) return entries;
	return entries.filter(
		(entry) =>
			entry.name.toLowerCase().includes(needle) ||
			entry.threads.some((t) => t.name.toLowerCase().includes(needle))
	);
}
