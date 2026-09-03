<script>
	import { api } from '$lib/api.js';
	import SearchHit from '$lib/SearchHit.svelte';
	import { formatCount, platformLabel } from '$lib/format.js';

	let query = $state('');
	let platform = $state('all');
	let result = $state(null);
	let loading = $state(false);
	let error = $state(null);
	let offset = $state(0);

	const LIMIT = 60;
	const filters = ['all', 'discord', 'facebook', 'instagram'];

	// Which words the server widened to a whole Czech paradigm, named by their
	// lemma - "hospody" was searched as every form of "hospoda".
	const widened = $derived(
		(result?.terms ?? [])
			.filter((t) => t.forms > 1)
			.map((t) => t.lemmas.join('/'))
			.join(', ')
	);

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
			placeholder="Search every message… (any inflected form; diacritics are ignored)"
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

	<p class="syntax">
		<code>pivo hospoda</code> both ·
		<code>pivo OR hospoda</code> either ·
		<code>(pivo OR víno) hospoda</code> grouped ·
		<code>pivo -hospoda</code> excluded ·
		<code>"hospody"</code> this exact form ·
		<code>hospod*</code> starts with
	</p>

	{#if error}
		<p class="note error">{error}</p>
	{:else if loading && !result}
		<p class="note">Searching…</p>
	{:else if result}
		<p class="note count">
			{formatCount(result.total)} results
			{#if widened}
				<span class="widened">· also in every form of {widened}</span>
			{/if}
		</p>
		<ul>
			{#each result.hits as hit (hit.message_id)}
				<li>
					<SearchHit {hit} href="/t/{hit.channel_id}?at={hit.message_id}" />
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

	.syntax {
		margin: 0 0 14px;
		font-size: 12px;
		color: var(--muted);
		line-height: 1.9;
	}

	.syntax code {
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 4px;
		padding: 1px 5px;
		font-size: 11px;
	}

	.widened {
		color: var(--muted);
	}

	.more {
		display: block;
		margin: 16px auto;
	}
</style>
