<script>
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { api } from '$lib/api.js';

	let { children } = $props();

	const links = [
		{ href: '/', label: 'Conversations' },
		{ href: '/search', label: 'Search' },
		{ href: '/people', label: 'People' },
		{ href: '/ingest', label: 'Import' }
	];

	let path = $derived($page.url.pathname);
	const isActive = (href) => (href === '/' ? path === '/' : path.startsWith(href));

	// The archive lives outside this project, so it may not be connected yet.
	// Every page needs it, so the check belongs here rather than in each route.
	let connected = $state(null); // null while we do not know yet

	$effect(() => {
		const here = path; // re-check on every navigation, not just on mount
		api
			.dbStatus()
			.then((status) => {
				connected = status.connected;
				if (!status.connected && here !== '/connect') goto('/connect');
			})
			.catch(() => (connected = false));
	});
</script>

<div class="shell">
	<nav>
		<a class="brand" href="/">Chat<span>Archive</span></a>
		<div class="links">
			{#if connected !== false}
				{#each links as link (link.href)}
					<a href={link.href} class:active={isActive(link.href)}>{link.label}</a>
				{/each}
			{/if}
		</div>
	</nav>
	<main>
		{#if connected === false && path !== '/connect'}
			<p class="gate">No database connected. <a href="/connect">Connect one</a>.</p>
		{:else if connected !== null || path === '/connect'}
			{@render children()}
		{/if}
	</main>
</div>

<style>
	:global(:root) {
		--bg: #0f1115;
		--panel: #171a21;
		--panel-2: #1e222b;
		--line: #2a2f3a;
		--text: #e6e8ee;
		--muted: #8b93a7;
		--accent: #6ea8ff;
		--mine: #2b4a7d;
		/* Timeline authorship: me on the left in blue, them on the right in pink. */
		--me: #3d9bff;
		--them: #ff5fa2;
		--radius: 10px;
		color-scheme: dark;
	}

	@media (prefers-color-scheme: light) {
		:global(:root) {
			--bg: #f6f7f9;
			--panel: #ffffff;
			--panel-2: #eef1f6;
			--line: #dfe3ea;
			--text: #14171f;
			--muted: #64708a;
			--accent: #1f6feb;
			--mine: #d3e3ff;
			--me: #1f6feb;
			--them: #e0357f;
			color-scheme: light;
		}
	}

	:global(*) {
		box-sizing: border-box;
	}

	:global(body) {
		margin: 0;
		background: var(--bg);
		color: var(--text);
		font: 15px/1.5 'Segoe UI', system-ui, -apple-system, sans-serif;
	}

	:global(a) {
		color: var(--accent);
	}

	:global(mark) {
		background: #ffd76a;
		color: #1a1200;
		border-radius: 3px;
		padding: 0 2px;
	}

	:global(button) {
		font: inherit;
		cursor: pointer;
		border: 1px solid var(--line);
		background: var(--panel-2);
		color: var(--text);
		border-radius: 8px;
		padding: 7px 13px;
	}

	:global(button:hover:not(:disabled)) {
		border-color: var(--accent);
	}

	:global(button:disabled) {
		opacity: 0.5;
		cursor: default;
	}

	:global(input[type='text'], input[type='search']) {
		font: inherit;
		background: var(--panel-2);
		border: 1px solid var(--line);
		color: var(--text);
		border-radius: 8px;
		padding: 8px 12px;
	}

	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
	}

	nav {
		display: flex;
		align-items: center;
		gap: 24px;
		padding: 0 18px;
		height: 52px;
		border-bottom: 1px solid var(--line);
		background: var(--panel);
		flex: none;
	}

	.brand {
		font-weight: 700;
		letter-spacing: -0.3px;
		text-decoration: none;
		color: var(--text);
	}

	.brand span {
		color: var(--accent);
	}

	.links {
		display: flex;
		gap: 4px;
	}

	.links a {
		text-decoration: none;
		color: var(--muted);
		padding: 6px 12px;
		border-radius: 8px;
	}

	.links a:hover {
		background: var(--panel-2);
		color: var(--text);
	}

	.links a.active {
		background: var(--panel-2);
		color: var(--text);
	}

	main {
		flex: 1;
		min-height: 0;
		overflow: hidden;
	}

	.gate {
		color: var(--muted);
		text-align: center;
		padding: 60px 0;
	}
</style>
