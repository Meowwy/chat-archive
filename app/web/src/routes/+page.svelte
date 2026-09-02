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

	let visible = $derived(
		query.trim()
			? threads.filter((t) => t.name.toLowerCase().includes(query.trim().toLowerCase()))
			: threads
	);
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
			{#each visible as thread (thread.id)}
				<li>
					<a href="/t/{thread.id}">
						<div class="avatar" style:--c={platformColor(thread.platform)}>
							{#if thread.avatar_sha256}
								<img src={mediaUrl(thread.avatar_sha256)} alt="" loading="lazy" />
							{:else}
								{thread.name.trim().charAt(0).toUpperCase() || '?'}
							{/if}
						</div>
						<div class="body">
							<div class="top">
								<span class="name">{thread.name}</span>
								<span class="dot" style:--c={platformColor(thread.platform)}></span>
								{#if thread.kind === 'GROUP'}
									<span class="tag">{thread.participants.length} people</span>
								{/if}
								<span class="when">{relativeDay(thread.last_ts)}</span>
							</div>
							<div class="preview">{thread.preview || '—'}</div>
						</div>
						<div class="count">{formatCount(thread.messages)}</div>
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
