<script>
	import { api, mediaUrl } from '$lib/api.js';
	import MonthChart from '$lib/MonthChart.svelte';
	import WordPanel from '$lib/WordPanel.svelte';
	import { filterEntries, groupThreads } from '$lib/groups.js';
	import { formatCount, formatMonthShort, monthKeys, platformColor } from '$lib/format.js';

	let threads = $state([]);
	let listError = $state(null);
	let loadingList = $state(true);

	let filter = $state('');
	let selectedKey = $state(null);
	let split = $state(false);
	let selfName = $state('me');

	let totals = $state(null);
	let totalsError = $state(null);
	let loadingTotals = $state(false);

	let word = $state('');
	let wordResult = $state(null);
	let wordError = $state(null);
	let loadingWord = $state(false);

	let entries = $derived(
		[...groupThreads(threads)].sort((a, b) => b.messages - a.messages)
	);
	let visible = $derived(filterEntries(entries, filter));
	let selected = $derived(entries.find((entry) => entry.key === selectedKey) ?? null);
	let ids = $derived(selected ? selected.threads.map((t) => t.id).join(',') : '');

	$effect(() => {
		api
			.threads({ platform: 'all' })
			.then((data) => {
				threads = data;
				listError = null;
			})
			.catch((e) => (listError = e.message))
			.finally(() => (loadingList = false));
		// Whose messages the blue half of a split line is.
		api
			.people()
			.then((data) => {
				selfName = data.people.find((p) => p.is_self)?.display ?? 'me';
			})
			.catch(() => {});
	});

	// The whole history of whoever is picked. It does not depend on the word, so
	// it is fetched once per person and stays put as words are typed.
	$effect(() => {
		const scope = ids;
		if (!scope) {
			totals = null;
			return;
		}
		let cancelled = false;
		loadingTotals = true;
		totals = null;
		api
			.monthStats({ threads: scope })
			.then((data) => {
				if (cancelled) return;
				totals = data;
				totalsError = null;
			})
			.catch((e) => !cancelled && (totalsError = e.message))
			.finally(() => !cancelled && (loadingTotals = false));
		return () => (cancelled = true);
	});

	// The same months, counted for one search. It re-runs when the person
	// changes too, so a word stays applied as you move down the list.
	$effect(() => {
		const scope = ids;
		const text = word.trim();
		if (!scope || !text) {
			wordResult = null;
			wordError = null;
			return;
		}
		let cancelled = false;
		loadingWord = true;
		api
			.monthStats({ threads: scope, q: text })
			.then((data) => {
				if (cancelled) return;
				wordResult = data;
				wordError = null;
			})
			.catch((e) => {
				if (cancelled) return;
				wordError = e.message;
				wordResult = null;
			})
			.finally(() => !cancelled && (loadingWord = false));
		return () => (cancelled = true);
	});

	/** Every month between the first and the last, so silent months plot as zero. */
	let axis = $derived.by(() => {
		const rows = totals?.months ?? [];
		return rows.length ? monthKeys(rows[0].month, rows[rows.length - 1].month) : [];
	});

	function align(rows) {
		const found = new Map((rows ?? []).map((row) => [row.month, row]));
		return axis.map((month) => found.get(month) ?? { month, messages: 0, mine: 0, theirs: 0 });
	}

	let totalSeries = $derived(align(totals?.months));
	let wordSeries = $derived(wordResult ? align(wordResult.months) : null);
	// A word is a filter, not an overlay: what it counted is the whole chart.
	let series = $derived(wordSeries ?? totalSeries);

	let names = $derived({
		mine: selfName,
		theirs: selected ? (selected.group ? 'everyone else' : selected.name) : 'them'
	});

	const yearOf = (key) => `${formatMonthShort(key)} ${key.slice(0, 4)}`;
	let span = $derived(axis.length ? `${yearOf(axis[0])} – ${yearOf(axis.at(-1))}` : '');
	let peak = $derived(series.reduce((most, m) => Math.max(most, m.messages), 0));
</script>

<div class="stats">
	<aside class="list">
		<div class="head">
			<h2>People</h2>
		</div>
		<div class="find">
			<input type="search" placeholder="Filter…" bind:value={filter} />
		</div>
		<div class="rows">
			{#if listError}
				<p class="note error">{listError}</p>
			{:else if loadingList}
				<p class="note">Loading…</p>
			{:else if visible.length === 0}
				<p class="note">No conversations.</p>
			{:else}
				<ul>
					{#each visible as entry (entry.key)}
						<li>
							<button
								class:on={entry.key === selectedKey}
								onclick={() => (selectedKey = entry.key)}
							>
								<span class="avatar" style:--c={platformColor(entry.newest.platform)}>
									{#if entry.newest.avatar_sha256}
										<img src={mediaUrl(entry.newest.avatar_sha256)} alt="" loading="lazy" />
									{:else}
										{entry.name.trim().charAt(0).toUpperCase() || '?'}
									{/if}
								</span>
								<span class="who">
									<span class="name">{entry.name}</span>
									{#if entry.threads.length > 1}
										<span class="tag">{entry.threads.length} chats</span>
									{:else if entry.group}
										<span class="tag">{entry.group} people</span>
									{/if}
								</span>
								<span class="count">{formatCount(entry.messages)}</span>
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	</aside>

	<section class="board">
		{#if !selected}
			<p class="note big">Pick someone on the left to see their messages over time.</p>
		{:else}
			<header>
				<div>
					<h1>{selected.name}</h1>
					<p class="sub">
						{#if wordResult}
							<b>{formatCount(wordResult.total)}</b> of {formatCount(totals?.total ?? 0)} messages used
							<b>“{wordResult.query}”</b>
						{:else if totals}
							<b>{formatCount(totals.total)}</b> messages
						{:else}
							Loading…
						{/if}
						{#if span}<span class="span">· {span}</span>{/if}
					</p>
				</div>
				<div class="tabs">
					<button class:on={!split} onclick={() => (split = false)}>Combined</button>
					<button class:on={split} onclick={() => (split = true)}>Split by author</button>
				</div>
			</header>

			{#if totalsError}
				<p class="note error">{totalsError}</p>
			{:else if loadingTotals}
				<p class="note">Loading…</p>
			{:else if !axis.length}
				<p class="note">No messages in this conversation.</p>
			{:else}
				<MonthChart
					months={series}
					{split}
					{names}
					label={wordResult ? `“${wordResult.query}”` : 'messages'}
				/>
				<p class="foot">
					{#if wordResult}
						Messages using the word each month.
					{:else}
						Messages each month. Count a word on the right to plot just those.
					{/if}
					· busiest month {formatCount(peak)}
				</p>
			{/if}
		{/if}
	</section>

	<WordPanel
		result={wordResult}
		loading={loadingWord}
		error={wordError}
		disabled={!selected}
		{names}
		onquery={(text) => (word = text)}
	/>
</div>

<style>
	.stats {
		display: grid;
		grid-template-columns: 300px 1fr 320px;
		height: 100%;
		min-height: 0;
	}

	.list {
		display: flex;
		flex-direction: column;
		min-height: 0;
		border-right: 1px solid var(--line);
		background: var(--panel);
	}

	.head {
		padding: 10px 12px 0;
	}

	h2 {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--muted);
		margin: 0;
	}

	.find {
		padding: 8px 12px 10px;
		border-bottom: 1px solid var(--line);
	}

	.find input {
		width: 100%;
	}

	.rows {
		flex: 1;
		overflow-y: auto;
		padding: 8px;
	}

	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.rows button {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		text-align: left;
		border: 1px solid transparent;
		background: none;
		padding: 6px 8px;
		border-radius: 8px;
	}

	.rows button:hover {
		background: var(--panel-2);
	}

	.rows button.on {
		background: var(--panel-2);
		border-color: var(--accent);
	}

	.avatar {
		width: 30px;
		height: 30px;
		flex: none;
		border-radius: 50%;
		background: var(--bg);
		border: 2px solid var(--c);
		display: grid;
		place-items: center;
		font-size: 12px;
		font-weight: 600;
		overflow: hidden;
	}

	.avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.who {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: baseline;
		gap: 6px;
	}

	.name {
		font-size: 13px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.tag {
		font-size: 11px;
		color: var(--muted);
		white-space: nowrap;
	}

	.count {
		font-size: 12px;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
		flex: none;
	}

	.board {
		display: flex;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
		padding: 16px 20px 14px;
	}

	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		flex-wrap: wrap;
		margin-bottom: 10px;
	}

	h1 {
		font-size: 17px;
		margin: 0;
		overflow-wrap: anywhere;
	}

	.sub {
		margin: 3px 0 0;
		font-size: 13px;
		color: var(--muted);
	}

	.sub b {
		color: var(--text);
		font-weight: 600;
	}

	.span {
		white-space: nowrap;
	}

	.tabs {
		display: flex;
		gap: 4px;
		flex: none;
	}

	.tabs button {
		font-size: 12px;
	}

	.tabs button.on {
		border-color: var(--accent);
		color: var(--accent);
	}

	.foot {
		margin: 10px 0 0;
		font-size: 12px;
		color: var(--muted);
	}

	.note {
		color: var(--muted);
		text-align: center;
		padding: 30px 0;
	}

	.note.big {
		margin: auto;
		font-size: 14px;
	}

	.error {
		color: #ff8a8a;
	}

	@media (max-width: 1200px) {
		.stats {
			grid-template-columns: 250px 1fr 280px;
		}
	}

	@media (max-width: 760px) {
		.stats {
			grid-template-columns: 1fr;
		}
		.list {
			display: none;
		}
	}
</style>
