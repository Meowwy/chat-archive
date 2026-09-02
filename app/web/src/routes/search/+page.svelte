<script>
	import { api } from '$lib/api.js';
	import { formatCount, formatDayShort, platformColor, platformLabel } from '$lib/format.js';

	let query = $state('');
	let platform = $state('all');
	let result = $state(null);
	let loading = $state(false);
	let error = $state(null);
	let offset = $state(0);

	const LIMIT = 60;
	const filters = ['all', 'discord', 'facebook', 'instagram'];

	async function run(newOffset = 0) {
		const text = query.trim();
		if (!text) {
			result = null;
			return;
		}
		loading = true;
		error = null;
		try {
			const data = await api.search({ q: text, platform, limit: LIMIT, offset: newOffset });
			offset = newOffset;
			result = newOffset === 0 ? data : { ...data, hits: [...result.hits, ...data.hits] };
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	/**
	 * The API returns a snippet with <mark> tags already applied; split it so the
	 * template can render the marked runs without injecting raw HTML.
	 */
	function segments(snippet) {
		return snippet
			.split(/(<mark>.*?<\/mark>)/g)
			.filter(Boolean)
			.map((chunk) =>
				chunk.startsWith('<mark>')
					? { hit: true, text: chunk.slice(6, -7) }
					: { hit: false, text: chunk }
			);
	}
</script>

<div class="wrap">
	<form
		onsubmit={(e) => {
			e.preventDefault();
			run(0);
		}}
	>
		<input
			type="search"
			placeholder="Search every message… (diacritics are ignored)"
			bind:value={query}
			{@attach (node) => node.focus()}
		/>
		<div class="tabs">
			{#each filters as f (f)}
				<button
					type="button"
					class:on={platform === f}
					onclick={() => {
						platform = f;
						run(0);
					}}
				>
					{f === 'all' ? 'All' : platformLabel(f)}
				</button>
			{/each}
		</div>
		<button type="submit" disabled={loading}>Search</button>
	</form>

	{#if error}
		<p class="note error">{error}</p>
	{:else if loading && !result}
		<p class="note">Searching…</p>
	{:else if result}
		<p class="note count">
			{formatCount(result.total)} results
		</p>
		<ul>
			{#each result.hits as hit (hit.message_id)}
				<li>
					<a href="/t/{hit.channel_id}?at={hit.message_id}">
						<div class="top">
							<span class="dot" style:--c={platformColor(hit.platform)}></span>
							<span class="thread">{hit.thread_name}</span>
							<span class="sender">{hit.sender}</span>
							<span class="when">{formatDayShort(hit.timestamp)}</span>
						</div>
						<p class="snippet">
							{#each segments(hit.snippet) as part, i (i)}
								{#if part.hit}<mark>{part.text}</mark>{:else}{part.text}{/if}
							{/each}
						</p>
					</a>
				</li>
			{/each}
		</ul>
		{#if result.hits.length < result.total}
			<button class="more" onclick={() => run(offset + LIMIT)} disabled={loading}>
				{loading ? 'Loading…' : 'Load more'}
			</button>
		{/if}
	{:else}
		<p class="note">Type something to search for.</p>
	{/if}
</div>

<style>
	.wrap {
		height: 100%;
		overflow-y: auto;
		max-width: 900px;
		margin: 0 auto;
		padding: 20px 18px 60px;
	}

	form {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
		position: sticky;
		top: 0;
		background: var(--bg);
		padding-bottom: 12px;
		z-index: 2;
	}

	form input {
		flex: 1;
		min-width: 240px;
	}

	.tabs {
		display: flex;
		gap: 4px;
	}

	.tabs button.on {
		border-color: var(--accent);
		color: var(--accent);
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
		display: block;
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

	.top {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 12px;
		color: var(--muted);
	}

	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--c);
		flex: none;
	}

	.thread {
		font-weight: 600;
		color: var(--text);
	}

	.when {
		margin-left: auto;
	}

	.snippet {
		margin: 4px 0 0;
		overflow-wrap: anywhere;
	}

	.note {
		color: var(--muted);
		padding: 20px 0;
		text-align: center;
	}

	.note.count {
		text-align: left;
		padding: 4px 0 10px;
		font-size: 13px;
	}

	.error {
		color: #ff8a8a;
	}

	.more {
		display: block;
		margin: 16px auto;
	}
</style>
