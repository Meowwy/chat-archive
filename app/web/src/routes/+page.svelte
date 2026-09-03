<script>
	import { api, mediaUrl } from '$lib/api.js';
	import { formatCount, platformColor, platformLabel, relativeDay } from '$lib/format.js';

	let threads = $state([]);
	let stats = $state(null);
	let platform = $state('all');
	let query = $state('');
	let loading = $state(true);
	let error = $state(null);

	const filters = ['all', 'discord', 'facebook', 'instagram'];

	$effect(() => {
		const current = platform;
		let cancelled = false;
		loading = true;
		api
			.threads({ platform: current })
			.then((data) => {
				if (!cancelled) {
					threads = data;
					error = null;
				}
			})
			.catch((e) => !cancelled && (error = e.message))
			.finally(() => !cancelled && (loading = false));
		return () => (cancelled = true);
	});

	$effect(() => {
		api.stats().then((data) => (stats = data));
	});

	// One row per person, not per chat: somebody you talk to on both Discord and
	// Instagram is one entry under the name you gave them. Anything without a
	// single mapped counterpart - group chats, unmapped identities - stays as
	// its own row.
	let entries = $derived.by(() => {
		const people = new Map();
		const output = [];
		for (const thread of threads) {
			if (thread.person_id == null) {
				output.push({
					key: `t${thread.id}`,
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
					name: thread.person,
					href: `/t/${thread.id}`,
					threads: [],
					messages: 0,
					last_ts: 0,
					newest: thread,
					group: 0
				};
				people.set(thread.person_id, entry);
				output.push(entry);
			}
			entry.threads.push(thread);
			entry.messages += thread.messages;
			if (thread.last_ts > entry.last_ts) {
				entry.last_ts = thread.last_ts;
				entry.newest = thread;
				entry.href = `/t/${thread.id}`;
			}
		}
		return output.sort((a, b) => b.last_ts - a.last_ts);
	});

	let visible = $derived.by(() => {
		const needle = query.trim().toLowerCase();
		if (!needle) return entries;
		return entries.filter(
			(entry) =>
				entry.name.toLowerCase().includes(needle) ||
				entry.threads.some((t) => t.name.toLowerCase().includes(needle))
		);
	});
</script>

<div class="wrap">
	<header>
		<div class="totals">
			{#if stats}
				<strong>{formatCount(stats.total)}</strong> messages
				{#each stats.platforms as p (p.platform)}
					<span class="chip" style:--c={platformColor(p.platform)}>
						{platformLabel(p.platform)} · {formatCount(p.messages)}
					</span>
				{/each}
			{/if}
		</div>
		<div class="controls">
			<div class="tabs">
				{#each filters as f (f)}
					<button class:on={platform === f} onclick={() => (platform = f)}>
						{f === 'all' ? 'All' : platformLabel(f)}
					</button>
				{/each}
			</div>
			<input type="search" placeholder="Filter conversations…" bind:value={query} />
		</div>
	</header>

	{#if error}
		<p class="msg error">{error}</p>
	{:else if loading}
		<p class="msg">Loading…</p>
	{:else if visible.length === 0}
		<p class="msg">No conversations.</p>
	{:else}
		<ul>
			{#each visible as entry (entry.key)}
				<li>
					<a href={entry.href}>
						<div class="avatar" style:--c={platformColor(entry.newest.platform)}>
							{#if entry.newest.avatar_sha256}
								<img src={mediaUrl(entry.newest.avatar_sha256)} alt="" loading="lazy" />
							{:else}
								{entry.name.trim().charAt(0).toUpperCase() || '?'}
							{/if}
						</div>
						<div class="body">
							<div class="top">
								<span class="name">{entry.name}</span>
								{#each entry.threads as thread (thread.id)}
									<span
										class="dot"
										style:--c={platformColor(thread.platform)}
										title={platformLabel(thread.platform)}
									></span>
								{/each}
								{#if entry.threads.length > 1}
									<span class="tag">{entry.threads.length} chats</span>
								{:else if entry.group}
									<span class="tag">{entry.group} people</span>
								{/if}
								<span class="when">{relativeDay(entry.last_ts)}</span>
							</div>
							<div class="preview">{entry.newest.preview || '—'}</div>
						</div>
						<div class="count">{formatCount(entry.messages)}</div>
					</a>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.wrap {
		height: 100%;
		overflow-y: auto;
		max-width: 940px;
		margin: 0 auto;
		padding: 20px 18px 60px;
	}

	header {
		position: sticky;
		top: 0;
		background: var(--bg);
		padding-bottom: 12px;
		z-index: 2;
	}

	.totals {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		color: var(--muted);
		font-size: 13px;
		min-height: 24px;
	}

	.totals strong {
		color: var(--text);
		font-size: 15px;
	}

	.chip {
		border: 1px solid var(--line);
		border-left: 3px solid var(--c);
		border-radius: 6px;
		padding: 2px 8px;
	}

	.controls {
		display: flex;
		gap: 10px;
		margin-top: 12px;
		flex-wrap: wrap;
	}

	.tabs {
		display: flex;
		gap: 4px;
	}

	.tabs button.on {
		border-color: var(--accent);
		color: var(--accent);
	}

	.controls input {
		flex: 1;
		min-width: 180px;
	}

	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	li a {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 10px 12px;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		text-decoration: none;
		color: inherit;
	}

	li a:hover {
		border-color: var(--accent);
	}

	.avatar {
		width: 42px;
		height: 42px;
		flex: none;
		border-radius: 50%;
		background: var(--panel-2);
		border: 2px solid var(--c);
		display: grid;
		place-items: center;
		font-weight: 600;
		overflow: hidden;
	}

	.avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.body {
		flex: 1;
		min-width: 0;
	}

	.top {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.name {
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--c);
		flex: none;
	}

	.tag,
	.when {
		font-size: 12px;
		color: var(--muted);
		white-space: nowrap;
	}

	.when {
		margin-left: auto;
	}

	.preview {
		color: var(--muted);
		font-size: 13px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		margin-top: 2px;
	}

	.count {
		font-variant-numeric: tabular-nums;
		color: var(--muted);
		font-size: 13px;
		flex: none;
	}

	.msg {
		color: var(--muted);
		padding: 30px 0;
		text-align: center;
	}

	.error {
		color: #ff8a8a;
	}
</style>
