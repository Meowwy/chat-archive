<script>
	import { formatDayShort, platformColor } from '$lib/format.js';

	/**
	 * One search result, shared by the global search page and the in-conversation
	 * panel. A hit is a link when it navigates and a button when the page it is
	 * already on can scroll to the message itself.
	 */
	let { hit, href = null, onclick = null, showThread = true } = $props();

	/**
	 * The API returns a snippet with <mark> tags already applied; split it so the
	 * template can render the marked runs without injecting raw HTML.
	 */
	function segments(snippet) {
		return (snippet ?? '')
			.split(/(<mark>.*?<\/mark>)/g)
			.filter(Boolean)
			.map((chunk) =>
				chunk.startsWith('<mark>')
					? { hit: true, text: chunk.slice(6, -7) }
					: { hit: false, text: chunk }
			);
	}
</script>

{#snippet body()}
	<div class="top">
		<span class="dot" style:--c={platformColor(hit.platform)}></span>
		{#if showThread}<span class="thread">{hit.thread_name}</span>{/if}
		<span class="sender">{hit.sender}</span>
		<span class="when">{formatDayShort(hit.timestamp)}</span>
	</div>
	<p class="snippet">
		{#each segments(hit.snippet) as part, i (i)}
			{#if part.hit}<mark>{part.text}</mark>{:else}{part.text}{/if}
		{/each}
	</p>
{/snippet}

{#if href}
	<a {href}>{@render body()}</a>
{:else}
	<button type="button" {onclick}>{@render body()}</button>
{/if}

<style>
	a,
	button {
		display: block;
		width: 100%;
		text-align: left;
		padding: 10px 12px;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		text-decoration: none;
		color: inherit;
		font: inherit;
	}

	a:hover,
	button:hover {
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
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.sender {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.when {
		margin-left: auto;
		white-space: nowrap;
	}

	.snippet {
		margin: 4px 0 0;
		overflow-wrap: anywhere;
	}
</style>
