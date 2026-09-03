<script>
	import { api } from '$lib/api.js';
	import SearchHit from '$lib/SearchHit.svelte';
	import { formatCount } from '$lib/format.js';

	/**
	 * Full-text search over one person's conversations, docked beside them.
	 *
	 * It searches every chat in the group, open or not - a hit in a hidden chat
	 * is exactly the reason to open it - and hands the click back to the page,
	 * which opens the column and scrolls it to the message.
	 */
	let { threads = [], showThread = false, onhit, onclose } = $props();

	const LIMIT = 40;
	const DEBOUNCE_MS = 300;

	let query = $state('');
	let result = $state(null);
	let loading = $state(false);
	let error = $state(null);
	let offset = $state(0);
	let active = $state(null);
	let timer = null;

	let ids = $derived(threads.map((t) => t.id).join(','));

	async function run(newOffset = 0) {
		clearTimeout(timer);
		const text = query.trim();
		if (!text) {
			result = null;
			error = null;
			return;
		}
		loading = true;
		error = null;
		try {
			const data = await api.search({ q: text, threads: ids, limit: LIMIT, offset: newOffset });
			offset = newOffset;
			result = newOffset === 0 ? data : { ...data, hits: [...result.hits, ...data.hits] };
		} catch (e) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	function typed() {
		clearTimeout(timer);
		timer = setTimeout(() => run(0), DEBOUNCE_MS);
	}

	function open(hit) {
		active = hit.message_id;
		onhit(hit);
	}
</script>

<aside class="panel">
	<div class="head">
		<h2>Search</h2>
		<button class="close" onclick={onclose} title="Close search">×</button>
	</div>

	<form
		onsubmit={(e) => {
			e.preventDefault();
			run(0);
		}}
	>
		<input
			type="search"
			placeholder="Search this conversation…"
			bind:value={query}
			oninput={typed}
			{@attach (node) => node.focus()}
		/>
	</form>

	<div class="results">
		{#if error}
			<p class="note error">{error}</p>
		{:else if loading && !result}
			<p class="note">Searching…</p>
		{:else if result}
			<p class="count">{formatCount(result.total)} results</p>
			<ul>
				{#each result.hits as hit (hit.message_id)}
					<li class:on={active === hit.message_id}>
						<SearchHit {hit} {showThread} onclick={() => open(hit)} />
					</li>
				{/each}
			</ul>
			{#if result.hits.length < result.total}
				<button class="more" onclick={() => run(offset + LIMIT)} disabled={loading}>
					{loading ? 'Loading…' : 'Load more'}
				</button>
			{/if}
		{:else}
			<p class="note">
				{threads.length > 1
					? `Search all ${threads.length} chats with this person.`
					: 'Search this conversation.'}
				<br />Any inflected form; diacritics are ignored.
				<br /><code>OR</code>, <code>-slovo</code> and brackets work here too.
			</p>
		{/if}
	</div>
</aside>

<style>
	.note code {
		background: var(--bg);
		border: 1px solid var(--line);
		border-radius: 4px;
		padding: 0 4px;
		font-size: 11px;
	}

	.panel {
		display: flex;
		flex-direction: column;
		min-height: 0;
		border-left: 1px solid var(--line);
		background: var(--panel);
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 12px 0;
	}

	h2 {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--muted);
		margin: 0;
	}

	.close {
		border: none;
		background: none;
		color: var(--muted);
		font-size: 18px;
		line-height: 1;
		padding: 0 2px;
	}

	.close:hover {
		color: var(--text);
	}

	form {
		padding: 8px 12px 10px;
		border-bottom: 1px solid var(--line);
	}

	form input {
		width: 100%;
	}

	.results {
		flex: 1;
		overflow-y: auto;
		padding: 10px 12px 30px;
	}

	.count {
		margin: 0 0 8px;
		font-size: 12px;
		color: var(--muted);
	}

	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	li.on {
		outline: 1px solid var(--accent);
		border-radius: var(--radius);
	}

	.note {
		color: var(--muted);
		font-size: 12px;
		padding: 16px 2px;
	}

	.error {
		color: #ff8a8a;
	}

	.more {
		display: block;
		margin: 14px auto;
		font-size: 12px;
	}

	/* Narrow windows keep the conversation and drop the side panels, the way
	   the sidebar already does. */
	@media (max-width: 760px) {
		.panel {
			display: none;
		}
	}
</style>
