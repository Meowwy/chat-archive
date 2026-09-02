<script>
	import { page } from '$app/stores';
	import { api, mediaUrl } from '$lib/api.js';
	import Message from '$lib/Message.svelte';
	import {
		dayKey,
		formatCount,
		formatDay,
		formatMonth,
		platformColor,
		platformLabel
	} from '$lib/format.js';

	const PAGE = 150;
	const NEAR_EDGE = 400;

	let threadId = $derived($page.params.id);
	let anchorId = $derived($page.url.searchParams.get('at'));

	let thread = $state(null);
	let messages = $state([]);
	let hasOlder = $state(true);
	let hasNewer = $state(false);
	let busy = $state(false);
	let error = $state(null);
	let highlight = $state(null);
	let lightbox = $state(null);
	let scroller = $state(null);

	// Reload whenever the route or the search anchor changes.
	$effect(() => {
		const id = threadId;
		const at = anchorId;
		let cancelled = false;

		thread = null;
		messages = [];
		error = null;
		highlight = at;

		(async () => {
			try {
				const [detail, page1] = await Promise.all([
					api.thread(id),
					api.messages(id, at ? { at, limit: PAGE } : { limit: PAGE })
				]);
				if (cancelled) return;
				thread = detail;
				messages = page1.messages;
				hasOlder = page1.messages.length > 0;
				hasNewer = Boolean(at);
				await tick();
				if (cancelled) return;
				if (at) scrollToMessage(at);
				else scrollToBottom();
			} catch (e) {
				if (!cancelled) error = e.message;
			}
		})();

		return () => (cancelled = true);
	});

	const tick = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));

	function scrollToBottom() {
		if (scroller) scroller.scrollTop = scroller.scrollHeight;
	}

	function scrollToMessage(id) {
		const element = document.getElementById(`m${id}`);
		if (element) element.scrollIntoView({ block: 'center' });
		else scrollToBottom();
	}

	async function loadOlder() {
		if (busy || !hasOlder || messages.length === 0) return;
		busy = true;
		const before = messages[0].timestamp;
		const anchorTop = scroller.scrollHeight - scroller.scrollTop;
		try {
			const data = await api.messages(threadId, { before, limit: PAGE });
			if (data.messages.length === 0) {
				hasOlder = false;
			} else {
				messages = [...data.messages, ...messages];
				await tick();
				// Keep the viewport pinned to the same message after prepending.
				scroller.scrollTop = scroller.scrollHeight - anchorTop;
			}
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
		}
	}

	async function loadNewer() {
		if (busy || !hasNewer || messages.length === 0) return;
		busy = true;
		const after = messages[messages.length - 1].timestamp;
		try {
			const data = await api.messages(threadId, { after, limit: PAGE });
			if (data.messages.length === 0) hasNewer = false;
			else messages = [...messages, ...data.messages];
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
		}
	}

	function onScroll() {
		if (!scroller) return;
		if (scroller.scrollTop < NEAR_EDGE) loadOlder();
		else if (
			hasNewer &&
			scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < NEAR_EDGE
		) {
			loadNewer();
		}
	}

	async function jumpToMonth(month) {
		busy = true;
		highlight = null;
		try {
			const data = await api.messages(threadId, { ts: month.first_ts, limit: PAGE });
			messages = data.messages;
			hasOlder = true;
			hasNewer = true;
			await tick();
			if (scroller) scroller.scrollTop = Math.max(0, scroller.scrollHeight / 2 - 200);
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
		}
	}

	async function jumpToEnd() {
		busy = true;
		highlight = null;
		try {
			const data = await api.messages(threadId, { limit: PAGE });
			messages = data.messages;
			hasOlder = true;
			hasNewer = false;
			await tick();
			scrollToBottom();
		} finally {
			busy = false;
		}
	}

	// Insert day separators and mark runs by the same sender.
	let rendered = $derived.by(() => {
		const output = [];
		let previous = null;
		for (const message of messages) {
			const key = dayKey(message.timestamp);
			if (!previous || dayKey(previous.timestamp) !== key) {
				output.push({ kind: 'day', key: `d${message.message_id}`, timestamp: message.timestamp });
				previous = null;
			}
			output.push({
				kind: 'message',
				key: message.message_id,
				message,
				grouped:
					previous !== null &&
					previous.sender_id === message.sender_id &&
					message.timestamp - previous.timestamp < 5 * 60 * 1000
			});
			previous = message;
		}
		return output;
	});

	let years = $derived.by(() => {
		if (!thread) return [];
		const map = new Map();
		for (const month of thread.months) {
			const year = month.month.slice(0, 4);
			if (!map.has(year)) map.set(year, []);
			map.get(year).push(month);
		}
		return [...map.entries()].map(([year, months]) => ({ year, months }));
	});
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape') lightbox = null;
	}}
/>

<div class="thread">
	<aside>
		{#if thread}
			<div class="head">
				<div class="avatar" style:--c={platformColor(thread.platform)}>
					{#if thread.avatar_sha256}
						<img src={mediaUrl(thread.avatar_sha256)} alt="" />
					{:else}
						{thread.name.trim().charAt(0).toUpperCase() || '?'}
					{/if}
				</div>
				<div>
					<h1>{thread.name}</h1>
					<p class="sub">
						{platformLabel(thread.platform)} · {formatCount(thread.messages)} messages
					</p>
				</div>
			</div>

			<button class="wide" onclick={jumpToEnd}>Jump to the end ↓</button>

			<h2>Participants</h2>
			<ul class="people">
				{#each thread.participants as person (person.id)}
					<li>
						<span class="pname">{person.name}</span>
						<span class="pcount">{formatCount(person.messages)}</span>
					</li>
				{/each}
			</ul>

			<h2>Timeline</h2>
			<div class="timeline">
				{#each years as group (group.year)}
					<details open={years.length <= 2}>
						<summary>{group.year}</summary>
						{#each group.months as month (month.month)}
							<button class="month" onclick={() => jumpToMonth(month)}>
								<span>{formatMonth(month.month)}</span>
								<span class="bar" style:--w="{Math.min(100, month.messages / 40)}%"></span>
								<span class="mcount">{formatCount(month.messages)}</span>
							</button>
						{/each}
					</details>
				{/each}
			</div>
		{/if}
	</aside>

	<section
		class="stream"
		bind:this={scroller}
		onscroll={onScroll}
		role="log"
		aria-label="Messages"
	>
		{#if error}
			<p class="note error">{error}</p>
		{:else if !thread}
			<p class="note">Loading…</p>
		{:else}
			{#if busy}<p class="note sticky">Loading…</p>{/if}
			{#each rendered as item (item.key)}
				{#if item.kind === 'day'}
					<div class="day"><span>{formatDay(item.timestamp)}</span></div>
				{:else}
					<Message
						message={item.message}
						grouped={item.grouped}
						highlighted={highlight === item.message.message_id}
						onimage={(a) => (lightbox = a)}
					/>
				{/if}
			{/each}
		{/if}
	</section>
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
		grid-template-columns: 270px 1fr;
		height: 100%;
	}

	aside {
		border-right: 1px solid var(--line);
		background: var(--panel);
		overflow-y: auto;
		padding: 14px;
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

	.wide {
		width: 100%;
		margin-top: 12px;
	}

	.people {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 13px;
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

	.timeline summary {
		cursor: pointer;
		font-weight: 600;
		font-size: 13px;
		padding: 4px 0;
	}

	.month {
		display: grid;
		grid-template-columns: 78px 1fr 40px;
		align-items: center;
		gap: 6px;
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

	.month > span:first-child {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		text-align: left;
	}

	.bar {
		height: 5px;
		border-radius: 3px;
		background: var(--accent);
		width: var(--w);
		min-width: 2px;
		opacity: 0.55;
	}

	.mcount {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.stream {
		overflow-y: auto;
		padding: 10px 24px 40px;
		display: flex;
		flex-direction: column;
	}

	.day {
		text-align: center;
		margin: 18px 0 6px;
		font-size: 12px;
		color: var(--muted);
	}

	.day span {
		background: var(--panel-2);
		border-radius: 999px;
		padding: 3px 12px;
	}

	.note {
		text-align: center;
		color: var(--muted);
		padding: 20px;
	}

	.note.sticky {
		position: sticky;
		top: 0;
		background: var(--bg);
		padding: 6px;
		font-size: 12px;
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

	@media (max-width: 760px) {
		.thread {
			grid-template-columns: 1fr;
		}
		aside {
			display: none;
		}
	}
</style>
