<script>
	import { api } from '$lib/api.js';
	import { formatCount, formatSize } from '$lib/format.js';

	let status = $state(null);
	let candidate = $state(null); // what the file dialog handed back
	let path = $state('');
	let busy = $state(false);
	let error = $state(null);

	$effect(() => {
		refresh();
	});

	async function refresh() {
		try {
			status = await api.dbStatus();
			error = null;
		} catch (e) {
			error = e.message;
		}
	}

	async function pick(create) {
		busy = true;
		error = null;
		candidate = null;
		try {
			const chosen = await api.pickDatabase(create);
			if (chosen.path) {
				candidate = chosen;
				path = chosen.path;
			}
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
		}
	}

	async function connect(create = false) {
		const target = path.trim();
		if (!target) return;
		busy = true;
		error = null;
		try {
			await api.connectDatabase(target, create);
			// A different database invalidates every page the app is holding, so
			// start over with a clean load rather than a client-side navigation.
			location.assign('/');
		} catch (e) {
			error = e.message;
			busy = false;
		}
	}
</script>

<div class="wrap">
	<h1>Connect a database</h1>
	<p class="lead">
		The archive is a single SQLite file, and it is not part of this project — it stays wherever you
		keep it. Point the app at one, or start an empty one and import your exports into it.
	</p>

	{#if status?.connected}
		<div class="current">
			<span class="tick">✓</span>
			<div>
				<b>Connected</b>
				<code>{status.path}</code>
				<span class="muted">
					{formatCount(status.messages)} messages · {formatSize(status.size)} · media in
					<code class="inline">{status.vault_path}</code>
				</span>
			</div>
			<a class="btn" href="/">Open the archive</a>
		</div>
	{/if}

	<div class="choices">
		<button class="choice" onclick={() => pick(false)} disabled={busy}>
			<b>Choose an existing archive…</b>
			<span>A <code class="inline">.sqlite</code> file you already have</span>
		</button>
		<button class="choice" onclick={() => pick(true)} disabled={busy}>
			<b>Start an empty archive…</b>
			<span>Creates the file, ready for an import</span>
		</button>
	</div>

	<div class="manual">
		<input
			type="text"
			placeholder="…or type the full path to a .sqlite file"
			bind:value={path}
			onkeydown={(e) => e.key === 'Enter' && connect(false)}
			disabled={busy}
		/>
		<button onclick={() => connect(false)} disabled={busy || !path.trim()}>Connect</button>
	</div>

	{#if candidate}
		<div class="card" class:bad={candidate.error}>
			<code>{candidate.path}</code>
			{#if candidate.create}
				<p>A new, empty archive will be created here.</p>
				<button class="go" onclick={() => connect(true)} disabled={busy}>
					Create it and connect
				</button>
			{:else if candidate.error}
				<p class="err">{candidate.error}</p>
			{:else if candidate.exists}
				<p>
					{formatCount(candidate.messages)} messages in {formatCount(candidate.threads)}
					conversations · {formatSize(candidate.size)}
				</p>
				<button class="go" onclick={() => connect(false)} disabled={busy}>Connect</button>
			{:else}
				<p class="err">That file does not exist.</p>
			{/if}
		</div>
	{/if}

	{#if error}<p class="err">{error}</p>{/if}

	<p class="note">
		The choice is remembered in <code class="inline">app/settings.local.json</code>, which stays on
		this machine. The <code class="inline">ARCHIVE_DB</code> environment variable overrides it, and
		<code class="inline">py -m archive db &lt;path&gt;</code> does the same job from a terminal.
	</p>
</div>

<style>
	.wrap {
		height: 100%;
		overflow-y: auto;
		max-width: 720px;
		margin: 0 auto;
		padding: 40px 18px 60px;
	}

	h1 {
		font-size: 22px;
		margin: 0 0 8px;
	}

	.lead {
		color: var(--muted);
		margin: 0 0 24px;
	}

	.current {
		display: flex;
		align-items: center;
		gap: 12px;
		border: 1px solid var(--line);
		border-left: 3px solid #3fb950;
		border-radius: var(--radius);
		background: var(--panel);
		padding: 12px 14px;
		margin-bottom: 24px;
	}

	.current > div {
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-width: 0;
		flex: 1;
	}

	.tick {
		color: #3fb950;
		font-size: 18px;
	}

	.btn {
		text-decoration: none;
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 7px 13px;
		color: var(--text);
		background: var(--panel-2);
		white-space: nowrap;
	}

	.btn:hover {
		border-color: var(--accent);
	}

	.choices {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.choice {
		display: flex;
		flex-direction: column;
		gap: 4px;
		align-items: flex-start;
		text-align: left;
		padding: 16px;
		background: var(--panel);
		border-radius: var(--radius);
	}

	.choice span {
		color: var(--muted);
		font-size: 13px;
	}

	.manual {
		display: flex;
		gap: 8px;
		margin: 14px 0 0;
	}

	.manual input {
		flex: 1;
	}

	.card {
		margin-top: 18px;
		border: 1px solid var(--line);
		border-left: 3px solid var(--accent);
		border-radius: var(--radius);
		background: var(--panel);
		padding: 12px 14px;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 8px;
	}

	.card.bad {
		border-left-color: #ff8a8a;
	}

	.card p {
		margin: 0;
		color: var(--muted);
	}

	.go {
		border-color: var(--accent);
		color: var(--accent);
	}

	code {
		font-size: 12px;
		color: var(--muted);
		word-break: break-all;
	}

	code.inline {
		background: var(--panel-2);
		border-radius: 4px;
		padding: 1px 5px;
	}

	.muted {
		color: var(--muted);
		font-size: 13px;
	}

	.err {
		color: #ff8a8a;
	}

	.note {
		color: var(--muted);
		font-size: 13px;
		margin-top: 32px;
		line-height: 1.7;
	}

	@media (max-width: 620px) {
		.choices {
			grid-template-columns: 1fr;
		}
	}
</style>
