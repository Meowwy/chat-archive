<script>
	import { tick } from 'svelte';
	import { page } from '$app/stores';
	import { api, mediaUrl } from '$lib/api.js';
	import SearchPanel from '$lib/SearchPanel.svelte';
	import ThreadColumn from '$lib/ThreadColumn.svelte';
	import {
		formatCount,
		formatMonthShort,
		monthStart,
		platformColor,
		platformLabel
	} from '$lib/format.js';

	let threadId = $derived($page.params.id);
	let anchorId = $derived($page.url.searchParams.get('at'));

	let thread = $state(null);
	let error = $state(null);
	let lightbox = $state(null);
	let shownIds = $state([]);
	let allParticipants = $state(false);
	let searching = $state(false);
	// One entry per visible chat, so the scrubber can drive them all at once.
	let columns = $state({});

	$effect(() => {
		const id = threadId;
		const at = anchorId;
		let cancelled = false;

		thread = null;
		error = null;

		api
			.thread(id)
			.then((detail) => {
				if (cancelled) return;
				thread = detail;
				// Arriving from a search hit means one message matters, so show
				// only its chat; arriving from the list means show the person.
				shownIds = at ? [id] : detail.group.threads.map((t) => t.id);
			})
			.catch((e) => !cancelled && (error = e.message));

		return () => (cancelled = true);
	});

	let siblings = $derived(thread?.group.threads ?? []);
	let shown = $derived(siblings.filter((t) => shownIds.includes(t.id)));
	let title = $derived(thread ? (thread.group.person ?? thread.name) : '');

	function toggle(id) {
		if (!shownIds.includes(id)) {
			// Rebuild from the sibling order so the columns never shuffle.
			shownIds = siblings.filter((t) => t.id === id || shownIds.includes(t.id)).map((t) => t.id);
		} else if (shownIds.length > 1) {
			shownIds = shownIds.filter((x) => x !== id);
		}
	}

	// ----------------------------------------------------------- timeline

	/** Month totals over the chats on show, split by who was talking. */
	let months = $derived.by(() => {
		const chosen = new Set(shownIds);
		const map = new Map();
		for (const row of thread?.months ?? []) {
			if (!chosen.has(row.channel_id)) continue;
			let month = map.get(row.month);
			if (!month) {
				month = { month: row.month, mine: 0, theirs: 0, messages: 0 };
				map.set(row.month, month);
			}
			month.mine += row.mine;
			month.theirs += row.theirs;
			month.messages += row.messages;
		}
		return [...map.values()].sort((a, b) => a.month.localeCompare(b.month));
	});

	// The busiest month on show fills the bar, so the shape rescales as chats
	// are toggled instead of staying pinned to some absolute ceiling.
	let peak = $derived(months.reduce((most, m) => Math.max(most, m.messages), 0));

	// Every bar is a share of the space left over by the counts beside it, so
	// that space has to be one width for the whole list: left to size itself
	// per row, a month whose figures run to more digits gets a shorter track,
	// and its bar reads as fewer messages than a quieter month's. The widest
	// label is measured off-screen and the column pinned to it.
	const labelWidth = (m) =>
		formatCount(m.messages).length + formatCount(m.mine).length + formatCount(m.theirs).length;

	let countWidth = $state(0);
	let widest = $derived(
		months.reduce((most, m) => (labelWidth(m) > labelWidth(most) ? m : most), {
			messages: 0,
			mine: 0,
			theirs: 0
		})
	);

	let years = $derived.by(() => {
		const map = new Map();
		for (const month of months) {
			const key = month.month.slice(0, 4);
			let year = map.get(key);
			if (!year) {
				year = { year: key, months: [], mine: 0, theirs: 0, messages: 0 };
				map.set(key, year);
			}
			year.months.push(month);
			year.mine += month.mine;
			year.theirs += month.theirs;
			year.messages += month.messages;
		}
		return [...map.values()];
	});

	let totals = $derived(
		months.reduce(
			(sum, m) => ({
				messages: sum.messages + m.messages,
				mine: sum.mine + m.mine,
				theirs: sum.theirs + m.theirs
			}),
			{ messages: 0, mine: 0, theirs: 0 }
		)
	);

	/** Who "me" and "them" are, for the legend. */
	let names = $derived.by(() => {
		const everyone = shown.flatMap((t) => t.participants);
		const others = everyone.filter((p) => !p.is_self);
		return {
			mine: everyone.find((p) => p.is_self)?.name ?? 'me',
			// In a group chat the pink half is the room, not one person.
			theirs: thread?.group.person ?? (others.length === 1 ? others[0].name : 'everyone else')
		};
	});

	/** Merge the participants of every visible chat, one row per person. */
	let participants = $derived.by(() => {
		const map = new Map();
		for (const t of shown) {
			for (const person of t.participants) {
				const key = person.person_id ?? `u${person.id}`;
				const seen = map.get(key);
				if (seen) seen.messages += person.messages;
				else map.set(key, { ...person, key });
			}
		}
		return [...map.values()].sort((a, b) => b.messages - a.messages);
	});

	// A 270-person group chat would push the timeline off the bottom of the
	// panel, and the timeline is what the panel is for.
	const FEW = 12;
	let visiblePeople = $derived(allParticipants ? participants : participants.slice(0, FEW));

	function jumpToMonth(month) {
		const ts = monthStart(month.month);
		for (const t of shown) columns[t.id]?.jumpTo(ts);
	}

	function jumpToYear(year) {
		jumpToMonth(year.months[0]);
	}

	/** Take a search hit to its message, opening its chat if it is hidden. */
	async function showHit(hit) {
		if (!shownIds.includes(hit.channel_id)) {
			toggle(hit.channel_id);
			// Wait for the column to mount before asking it to go anywhere.
			await tick();
		}
		columns[hit.channel_id]?.jumpToMessage(hit.message_id);
	}
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape') lightbox = null;
	}}
/>

<div class="thread" class:withsearch={searching && thread}>
	<aside>
		{#if thread}
			<div class="head">
				<div class="avatar" style:--c={platformColor(thread.platform)}>
					{#if thread.avatar_sha256}
						<img src={mediaUrl(thread.avatar_sha256)} alt="" />
					{:else}
						{title.trim().charAt(0).toUpperCase() || '?'}
					{/if}
				</div>
				<div>
					<h1>{title}</h1>
					<p class="sub">
						{#if siblings.length === 1}
							{platformLabel(thread.platform)} · {formatCount(totals.messages)} messages
						{:else}
							{formatCount(totals.messages)} messages in {shown.length} of {siblings.length} chats
						{/if}
					</p>
				</div>
			</div>

			{#if siblings.length > 1}
				<h2>Chats</h2>
				<ul class="chats">
					{#each siblings as chat (chat.id)}
						{@const on = shownIds.includes(chat.id)}
						<li>
							<label class:off={!on}>
								<input
									type="checkbox"
									checked={on}
									disabled={on && shownIds.length === 1}
									onchange={() => toggle(chat.id)}
								/>
								<span class="dot" style:--c={platformColor(chat.platform)}></span>
								<span class="cname">{chat.name}</span>
								<span class="ccount">{formatCount(chat.messages)}</span>
							</label>
						</li>
					{/each}
				</ul>
				<p class="hint">At least one chat stays open.</p>
			{/if}

			<h2>Participants</h2>
			<ul class="people">
				{#each visiblePeople as person (person.key)}
					<li>
						<span class="pname">{person.name}</span>
						<span class="pcount">{formatCount(person.messages)}</span>
					</li>
				{/each}
			</ul>
			{#if participants.length > FEW}
				<button class="more" onclick={() => (allParticipants = !allParticipants)}>
					{allParticipants
						? 'Show fewer'
						: `Show all ${formatCount(participants.length)} people`}
				</button>
			{/if}

			<button class="opensearch" onclick={() => (searching = !searching)}>
				{searching ? 'Close search' : 'Open search'}
			</button>

			<h2>Timeline</h2>
			<div class="legend">
				<span><i class="swatch me"></i>{names.mine}</span>
				<span><i class="swatch them"></i>{names.theirs}</span>
			</div>
			<div class="total">
				<span>All time</span>
				<span class="split">
					{formatCount(totals.messages)}
					<b>(<i class="me">{formatCount(totals.mine)}</i> /
						<i class="them">{formatCount(totals.theirs)}</i>)</b>
				</span>
			</div>

			<div class="timeline" style:--cw={countWidth ? `${countWidth + 1}px` : null}>
				<span class="split probe" aria-hidden="true" bind:clientWidth={countWidth}>
					{formatCount(widest.messages)}
					<b>(<i>{formatCount(widest.mine)}</i> /
						<i>{formatCount(widest.theirs)}</i>)</b>
				</span>
				{#each years as group (group.year)}
					<details open={years.length <= 2}>
						<summary>
							<span class="ylabel">{group.year}</span>
							<span class="split">
								{formatCount(group.messages)}
								<b>(<i class="me">{formatCount(group.mine)}</i> /
									<i class="them">{formatCount(group.theirs)}</i>)</b>
							</span>
						</summary>
						<button class="yjump" onclick={() => jumpToYear(group)}>
							Jump to {group.year} ↑
						</button>
						{#each group.months as month (month.month)}
							<button class="month" onclick={() => jumpToMonth(month)}>
								<span class="mlabel">{formatMonthShort(month.month)}</span>
								<span
									class="bar"
									style:--w="{peak ? (month.messages / peak) * 100 : 0}%"
									style:--p="{month.messages ? (month.mine / month.messages) * 100 : 0}%"
								></span>
								<span class="split">
									{formatCount(month.messages)}
									<b>(<i class="me">{formatCount(month.mine)}</i> /
										<i class="them">{formatCount(month.theirs)}</i>)</b>
								</span>
							</button>
						{/each}
					</details>
				{/each}
			</div>
		{/if}
	</aside>

	<div class="columns">
		{#if error}
			<p class="note error">{error}</p>
		{:else if !thread}
			<p class="note">Loading…</p>
		{:else}
			{#each shown as chat (chat.id)}
				<ThreadColumn
					bind:this={columns[chat.id]}
					thread={chat}
					startAt={chat.id === threadId ? anchorId : null}
					highlight={chat.id === threadId ? anchorId : null}
					showHeader={siblings.length > 1}
					onclose={shown.length > 1 ? toggle : null}
					onimage={(a) => (lightbox = a)}
				/>
			{/each}
		{/if}
	</div>

	{#if searching && thread}
		<SearchPanel
			threads={siblings}
			showThread={siblings.length > 1}
			onhit={showHit}
			onclose={() => (searching = false)}
		/>
	{/if}
</div>

{#if lightbox}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<div class="lightbox" onclick={() => (lightbox = null)}>
		<img src={mediaUrl(lightbox.sha256)} alt={lightbox.name} />
		<p>{lightbox.name}</p>
	</div>
{/if}

<style>
	.thread {
		display: grid;
		grid-template-columns: 340px 1fr;
		height: 100%;
		min-height: 0;
	}

	/* The search panel is the mirror of the sidebar: same width, other side. */
	.thread.withsearch {
		grid-template-columns: 340px 1fr 340px;
	}

	aside {
		border-right: 1px solid var(--line);
		background: var(--panel);
		overflow-y: auto;
		padding: 14px;
	}

	.columns {
		display: flex;
		min-width: 0;
		overflow: hidden;
	}

	.head {
		display: flex;
		gap: 10px;
		align-items: center;
	}

	.avatar {
		width: 44px;
		height: 44px;
		flex: none;
		border-radius: 50%;
		border: 2px solid var(--c);
		background: var(--panel-2);
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

	h1 {
		font-size: 16px;
		margin: 0;
		overflow-wrap: anywhere;
	}

	.sub {
		margin: 2px 0 0;
		font-size: 12px;
		color: var(--muted);
	}

	h2 {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--muted);
		margin: 18px 0 6px;
	}

	.chats,
	.people {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 13px;
	}

	.chats label {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 4px 4px;
		border-radius: 6px;
		cursor: pointer;
	}

	.chats label:hover {
		background: var(--panel-2);
	}

	.chats label.off {
		opacity: 0.5;
	}

	.chats input {
		width: 14px;
		height: 14px;
		flex: none;
		accent-color: var(--accent);
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--c);
		flex: none;
	}

	.cname {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ccount {
		margin-left: auto;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
		font-size: 12px;
	}

	.more {
		border: none;
		background: none;
		color: var(--muted);
		font-size: 12px;
		padding: 4px 0 0;
		text-align: left;
	}

	.more:hover {
		color: var(--accent);
	}

	.opensearch {
		display: block;
		width: 100%;
		margin-top: 14px;
		font-size: 12px;
	}

	.hint {
		margin: 4px 0 0;
		font-size: 11px;
		color: var(--muted);
	}

	.people li {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 3px 0;
	}

	.pname {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pcount {
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}

	.legend {
		display: flex;
		gap: 12px;
		font-size: 11px;
		color: var(--muted);
		margin-bottom: 6px;
	}

	.legend span {
		display: flex;
		align-items: center;
		gap: 5px;
		min-width: 0;
	}

	.swatch {
		width: 9px;
		height: 9px;
		border-radius: 2px;
		flex: none;
	}

	.swatch.me {
		background: var(--me);
	}

	.swatch.them {
		background: var(--them);
	}

	.total {
		display: flex;
		justify-content: space-between;
		gap: 8px;
		font-size: 12px;
		padding: 5px 4px;
		border-top: 1px solid var(--line);
		border-bottom: 1px solid var(--line);
	}

	.split {
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		text-align: right;
	}

	.split b {
		font-weight: 400;
		color: var(--muted);
		font-size: 11px;
	}

	/* The two halves of the bar, named in the numbers beside it. */
	.split i {
		font-style: normal;
	}

	.split i.me {
		color: var(--me);
	}

	.split i.them {
		color: var(--them);
	}

	.timeline summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		cursor: pointer;
		font-size: 13px;
		padding: 5px 0;
	}

	.ylabel {
		font-weight: 600;
	}

	.yjump {
		width: 100%;
		border: none;
		background: none;
		color: var(--muted);
		font-size: 11px;
		text-align: left;
		padding: 2px 0 4px 8px;
	}

	.yjump:hover {
		color: var(--accent);
	}

	.month {
		display: grid;
		grid-template-columns: 34px 1fr var(--cw, auto);
		align-items: center;
		gap: 7px;
		width: 100%;
		border: none;
		background: none;
		padding: 2px 0 2px 8px;
		font-size: 12px;
		color: var(--muted);
		border-radius: 4px;
	}

	.month:hover {
		background: var(--panel-2);
		color: var(--text);
	}

	.mlabel {
		text-align: left;
	}

	/* Off-screen twin of the longest counts label; its width sets --cw. */
	.probe {
		position: absolute;
		left: -9999px;
		top: 0;
		visibility: hidden;
	}

	/* One strip painted in two colours, not two boxes laid side by side: boxes
	   that meet on a fractional pixel - which most of these splits do, the share
	   being an arbitrary percentage - are rasterised independently and the join
	   can show as a step. A gradient with a hard stop is one paint, so the two
	   parts cannot come apart at any zoom or display scale. */
	.bar {
		height: 7px;
		border-radius: 3px;
		width: var(--w);
		min-width: 3px;
		background: linear-gradient(to right, var(--me) 0 var(--p), var(--them) var(--p) 100%);
	}

	.note {
		text-align: center;
		color: var(--muted);
		padding: 20px;
		width: 100%;
	}

	.error {
		color: #ff8a8a;
	}

	.lightbox {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.85);
		display: grid;
		place-content: center;
		gap: 10px;
		z-index: 20;
		cursor: zoom-out;
		padding: 30px;
	}

	.lightbox img {
		max-width: 90vw;
		max-height: 84vh;
		object-fit: contain;
	}

	.lightbox p {
		color: #ddd;
		text-align: center;
		margin: 0;
		font-size: 13px;
	}

	@media (max-width: 1200px) {
		.thread.withsearch {
			grid-template-columns: 290px 1fr 300px;
		}
	}

	@media (max-width: 760px) {
		.thread,
		.thread.withsearch {
			grid-template-columns: 1fr;
		}
		aside {
			display: none;
		}
	}
</style>
