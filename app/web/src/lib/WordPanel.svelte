<script>
	import { formatCount } from '$lib/format.js';

	/**
	 * The search box from a conversation, docked beside the chart instead.
	 *
	 * It runs the same query language and the same Czech widening; what comes
	 * back is a shape rather than a list of hits, so the panel reports what was
	 * counted and leaves the plotting to the chart. It is always on show - the
	 * chart is what it filters, so there is nothing to close it back to.
	 */
	let {
		result = null,
		loading = false,
		error = null,
		disabled = false,
		names = { mine: 'me', theirs: 'them' },
		onquery
	} = $props();

	const DEBOUNCE_MS = 300;

	// The box owns what has been typed and the page owns what has been asked
	// for: only a settled query is sent, so a chart is not redrawn per keystroke.
	let query = $state('');
	let timer = null;

	function typed() {
		clearTimeout(timer);
		timer = setTimeout(() => onquery(query), DEBOUNCE_MS);
	}

	function submit(event) {
		event.preventDefault();
		clearTimeout(timer);
		onquery(query);
	}

	// Which words the server widened to a whole Czech paradigm, named by their
	// lemma - "hospody" was counted as every form of "hospoda".
	let widened = $derived(
		(result?.terms ?? [])
			.filter((term) => term.forms > 1)
			.map((term) => term.lemmas.join('/'))
			.join(', ')
	);
</script>

<aside class="panel">
	<div class="head">
		<h2>Word</h2>
	</div>

	<form onsubmit={submit}>
		<input
			type="search"
			placeholder="Count a word…"
			bind:value={query}
			oninput={typed}
			{disabled}
		/>
	</form>

	<div class="results">
		{#if error}
			<p class="note error">{error}</p>
		{:else if loading}
			<p class="note">Counting…</p>
		{:else if result}
			<p class="total">
				{formatCount(result.total)}
				<span>{result.total === 1 ? 'message' : 'messages'}</span>
			</p>
			<p class="split">
				<i class="me">{formatCount(result.mine)}</i> {names.mine} ·
				<i class="them">{formatCount(result.theirs)}</i> {names.theirs}
			</p>
			{#if widened}
				<p class="note">Counted in every form of <b>{widened}</b>.</p>
			{/if}
			{#if result.total === 0}
				<p class="note">Never said in this conversation.</p>
			{/if}
		{:else}
			<p class="note">
				Type a word to plot how often it was used.
				<br />Any inflected form; diacritics are ignored.
				<br /><code>OR</code>, <code>-slovo</code> and brackets work here too.
			</p>
		{/if}
	</div>
</aside>

<style>
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
		padding: 12px 12px 30px;
	}

	.total {
		margin: 0;
		font-size: 22px;
		font-variant-numeric: tabular-nums;
	}

	.total span {
		font-size: 13px;
		color: var(--muted);
	}

	.split {
		margin: 2px 0 12px;
		font-size: 12px;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}

	.split i {
		font-style: normal;
	}

	.split i.me {
		color: var(--me);
	}

	.split i.them {
		color: var(--them);
	}

	.note {
		color: var(--muted);
		font-size: 12px;
		padding: 4px 2px;
		line-height: 1.8;
	}

	.note code {
		background: var(--bg);
		border: 1px solid var(--line);
		border-radius: 4px;
		padding: 0 4px;
		font-size: 11px;
	}

	.error {
		color: #ff8a8a;
	}

	/* Narrow windows keep the chart and drop the side panels, as the
	   conversation view already does. */
	@media (max-width: 760px) {
		.panel {
			display: none;
		}
	}
</style>
