<script>
	import { tick } from 'svelte';
	import { api } from '$lib/api.js';
	import Message from '$lib/Message.svelte';
	import { dayKey, formatCount, formatDay, platformColor, platformLabel } from '$lib/format.js';

	let {
		thread,
		startAt = null,
		highlight = null,
		showHeader = false,
		onimage,
		onclose
	} = $props();

	const PAGE = 150;
	const NEAR_EDGE = 400;
	// How long to keep re-anchoring after a jump. Images have no height until
	// their bytes arrive, so the list keeps growing for a beat after the DOM
	// itself has settled.
	const SETTLE_MS = 800;

	let messages = $state([]);
	let hasOlder = $state(true);
	let hasNewer = $state(false);
	let busy = $state(false);
	let error = $state(null);
	let atEnd = $state(true);
	let scroller = $state(null);
	// A message the search panel asked for, once this column has been told to
	// go there; it outlines the same way a search hit arrived at by URL does.
	let marked = $state(null);
	// Loads can overlap - the mount is still in flight when the search panel
	// asks for a message. Only the newest one is allowed to replace the list.
	let request = 0;
	// True while we are moving the scrollbar ourselves; the scroll handler must
	// not mistake that for the reader reaching an edge and paginate underneath.
	let anchoring = false;

	$effect(() => {
		// Both reads have to happen here: an effect only tracks what it touches
		// synchronously, and the fetch inside load() is already async.
		const id = thread.id;
		const at = startAt;
		marked = null;
		load(id, at ? { at } : {}, at ? toMessage(at) : toBottom, { newer: Boolean(at) });
	});

	/**
	 * Replace the visible page, then put the viewport where `place` says.
	 *
	 * Every entry point - the first load, the month scrubber, a search hit,
	 * jump to the end - goes through here, so a slower earlier request can
	 * never land on top of a newer one.
	 */
	async function load(id, params, place, { newer = false } = {}) {
		const mine = ++request;
		busy = true;
		try {
			const data = await api.messages(id, { ...params, limit: PAGE });
			if (mine !== request) return;
			messages = data.messages;
			hasOlder = data.messages.length > 0;
			hasNewer = newer;
			error = null;
			anchor(place);
		} catch (e) {
			if (mine === request) error = e.message;
		} finally {
			if (mine === request) busy = false;
		}
	}

	// ------------------------------------------------------------ scrolling

	const gap = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;

	const toBottom = () => {
		if (scroller) scroller.scrollTop = scroller.scrollHeight;
	};

	/** Put a given message at the top of the viewport. */
	const toMessage = (id) => () => {
		let element = scroller?.querySelector(`[data-mid="${id}"]`);
		if (!element) return;
		// Take the date chip with it when there is one, so you can see where
		// you have landed rather than just the message.
		const above = element.previousElementSibling;
		if (above?.classList.contains('day')) element = above;
		scroller.scrollTop +=
			element.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 8;
	};

	/** Put the first message at or after `ts` at the top of the viewport. */
	const toTimestamp = (ts) => () => {
		const found = messages.find((m) => m.timestamp >= ts) ?? messages[messages.length - 1];
		if (found) toMessage(found.message_id)();
	};

	/**
	 * Move the viewport once the DOM has caught up, and keep it there.
	 *
	 * `tick()` waits for Svelte to patch the list - a raw requestAnimationFrame
	 * never fires in a background tab, which is what made the month scrubber
	 * look like it did nothing. Late-loading images then change the heights
	 * above us, so we re-apply as each one lands.
	 */
	async function anchor(apply) {
		anchoring = true;
		await tick();
		apply();
		const again = () => apply();
		scroller?.addEventListener('load', again, true); // `load` does not bubble
		setTimeout(() => {
			scroller?.removeEventListener('load', again, true);
			anchoring = false;
			refreshAtEnd();
		}, SETTLE_MS);
	}

	function refreshAtEnd() {
		atEnd = scroller ? !hasNewer && gap() < 80 : true;
	}

	function onScroll() {
		if (!scroller) return;
		refreshAtEnd();
		if (anchoring) return;
		if (scroller.scrollTop < NEAR_EDGE) loadOlder();
		else if (hasNewer && gap() < NEAR_EDGE) loadNewer();
	}

	// ------------------------------------------------------------ paging

	async function loadOlder() {
		if (busy || !hasOlder || messages.length === 0) return;
		busy = true;
		const mine = request;
		const before = messages[0].timestamp;
		const anchorTop = scroller.scrollHeight - scroller.scrollTop;
		try {
			const data = await api.messages(thread.id, { before, limit: PAGE });
			if (mine !== request) return;
			if (data.messages.length === 0) {
				hasOlder = false;
			} else {
				messages = [...data.messages, ...messages];
				// Keep the viewport pinned to the same message after prepending.
				await tick();
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
		const mine = request;
		const after = messages[messages.length - 1].timestamp;
		try {
			const data = await api.messages(thread.id, { after, limit: PAGE });
			if (mine !== request) return;
			if (data.messages.length === 0) hasNewer = false;
			else messages = [...messages, ...data.messages];
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
			refreshAtEnd();
		}
	}

	// ------------------------------------------------------------ commands

	/** Show the moment `ts` - the sidebar drives every visible column with this. */
	export function jumpTo(ts) {
		marked = null;
		return load(thread.id, { ts }, toTimestamp(ts), { newer: true });
	}

	/** Show one message and outline it - used by the search panel. */
	export function jumpToMessage(id) {
		marked = id;
		return load(thread.id, { at: id }, toMessage(id), { newer: true });
	}

	export function jumpToEnd() {
		marked = null;
		return load(thread.id, {}, toBottom);
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
</script>

<div class="column">
	{#if showHeader}
		<header style:--c={platformColor(thread.platform)}>
			<span class="dot"></span>
			<span class="cname">{thread.name}</span>
			<span class="cmeta">{platformLabel(thread.platform)} · {formatCount(thread.messages)}</span>
			{#if onclose}
				<button class="hide" onclick={() => onclose(thread.id)} title="Hide this chat">×</button>
			{/if}
		</header>
	{/if}

	<section
		class="stream"
		bind:this={scroller}
		onscroll={onScroll}
		role="log"
		aria-label="Messages in {thread.name}"
	>
		{#if error}
			<p class="note error">{error}</p>
		{:else}
			{#if busy}<p class="note sticky">Loading…</p>{/if}
			{#each rendered as item (item.key)}
				{#if item.kind === 'day'}
					<div class="day"><span>{formatDay(item.timestamp)}</span></div>
				{:else}
					<Message
						message={item.message}
						grouped={item.grouped}
						highlighted={(marked ?? highlight) === item.message.message_id}
						{onimage}
					/>
				{/if}
			{/each}
		{/if}
	</section>

	{#if !atEnd}
		<button class="toend" onclick={jumpToEnd}>Jump to the end ↓</button>
	{/if}
</div>

<style>
	.column {
		position: relative;
		display: flex;
		flex-direction: column;
		min-width: 0;
		flex: 1 1 0;
		border-left: 1px solid var(--line);
	}

	.column:first-child {
		border-left: none;
	}

	header {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--line);
		background: var(--panel);
		font-size: 13px;
	}

	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--c);
		flex: none;
	}

	.cname {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.cmeta {
		color: var(--muted);
		font-size: 12px;
		white-space: nowrap;
		margin-left: auto;
	}

	.hide {
		border: none;
		background: none;
		color: var(--muted);
		font-size: 17px;
		line-height: 1;
		padding: 0 2px;
	}

	.hide:hover {
		color: var(--text);
	}

	.stream {
		flex: 1;
		overflow-y: auto;
		/* Chrome's scroll anchoring pulls the viewport back to whatever it
		   decided was the anchor, which fights every jump we make ourselves. */
		overflow-anchor: none;
		padding: 10px 20px 40px;
		display: flex;
		flex-direction: column;
	}

	.toend {
		position: absolute;
		bottom: 14px;
		left: 50%;
		transform: translateX(-50%);
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 6px 14px;
		font-size: 12px;
		box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
	}

	.toend:hover {
		border-color: var(--accent);
		color: var(--accent);
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
		z-index: 1;
	}

	.error {
		color: #ff8a8a;
	}
</style>
