<script>
	import { api, streamIngest } from '$lib/api.js';
	import { formatCount, formatDayShort, platformLabel } from '$lib/format.js';

	let path = $state('');
	let sources = $state([]);
	let detectError = $state(null);
	let picking = $state(false);
	let running = $state(false);
	let results = $state([]);
	let runError = $state(null);
	let history = $state(null);
	let database = $state(null);

	async function loadHistory() {
		history = await api.history();
	}

	$effect(() => {
		loadHistory();
		api.dbStatus().then((status) => (database = status));
	});

	async function pick() {
		picking = true;
		detectError = null;
		results = [];
		runError = null;
		try {
			const data = await api.pickFolder();
			if (data.path) {
				path = data.path;
				sources = data.sources ?? [];
				detectError = data.error ?? null;
			}
		} catch (e) {
			detectError = e.message;
		} finally {
			picking = false;
		}
	}

	async function inspect() {
		if (!path.trim()) return;
		detectError = null;
		results = [];
		try {
			const data = await api.inspect(path.trim());
			sources = data.sources ?? [];
			detectError = data.error ?? null;
		} catch (e) {
			detectError = e.message;
		}
	}

	async function run() {
		running = true;
		results = [];
		runError = null;
		try {
			for await (const event of streamIngest(path)) {
				if (event.event === 'error') runError = event.message;
				else if (event.event === 'source-done') results = [...results, event];
			}
		} catch (e) {
			runError = e.message;
		} finally {
			running = false;
			sources = [];
			await loadHistory();
		}
	}
</script>

<div class="wrap">
	<h1>Import exports</h1>
	<p class="lead">
		Pick the folder holding an export downloaded from Facebook or Instagram. The files are
		copied into the archive, so the original folder is no longer needed afterwards. Messages
		already in the archive are skipped.
	</p>

	{#if database}
		<div class="dbline">
			<span class="muted">Archive</span>
			<code>{database.path}</code>
			<a href="/connect">Change…</a>
		</div>
	{/if}

	<div class="picker">
		<button onclick={pick} disabled={picking || running}>
			{picking ? 'Opening…' : 'Choose a folder…'}
		</button>
		<input
			type="text"
			placeholder="…or paste a path here"
			bind:value={path}
			onchange={inspect}
			disabled={running}
		/>
		<button onclick={inspect} disabled={!path.trim() || running}>Check</button>
	</div>

	{#if detectError}
		<p class="note error">{detectError}</p>
	{/if}

	{#if sources.length}
		<div class="found">
			<h2>Found</h2>
			{#each sources as source (source.path)}
				<div class="card">
					<strong>{source.label}</strong>
					<span>{formatCount(source.threads)} conversations · {formatCount(source.messages)} messages</span>
					<code>{source.path}</code>
				</div>
			{/each}
			<button class="go" onclick={run} disabled={running}>
				{running ? 'Importing…' : 'Import'}
			</button>
		</div>
	{/if}

	{#if runError}
		<p class="note error">{runError}</p>
	{/if}

	{#if results.length}
		<h2>Result</h2>
		{#each results as result (result.kind)}
			<div class="card result">
				<strong>{result.label}</strong>
				<ul>
					<li>
						Conversations: {formatCount(result.stats.threads_seen)}
						({formatCount(result.stats.new_threads)} new)
					</li>
					<li>
						Messages: <b>{formatCount(result.stats.new_msgs)}</b> new,
						{formatCount(result.stats.dup_msgs)} already in the archive
					</li>
					<li>
						Attachments: {formatCount(result.stats.new_media)} stored,
						{formatCount(result.stats.dup_media)} already stored,
						{formatCount(result.stats.missing_media)} missing
					</li>
				</ul>
			</div>
		{/each}
	{/if}

	<h2>History</h2>
	{#if history}
		<table>
			<thead>
				<tr>
					<th>Source</th>
					<th>When</th>
					<th class="n">New</th>
					<th class="n">Duplicates</th>
					<th class="n">Attachments</th>
					<th>Status</th>
				</tr>
			</thead>
			<tbody>
				{#each history.ingest as run (run.run_id)}
					<tr>
						<td>{platformLabel(run.source_kind)}</td>
						<td>{formatDayShort(run.started_at)}</td>
						<td class="n">{formatCount(run.new_msgs)}</td>
						<td class="n">{formatCount(run.dup_msgs)}</td>
						<td class="n">{formatCount(run.new_media)}</td>
						<td class:bad={run.status !== 'ok'}>{run.status}</td>
					</tr>
				{/each}
				{#each history.sync as run (`s${run.run_id}`)}
					<tr class="sync">
						<td>Discord (sync_dht)</td>
						<td>{formatDayShort(run.started_at)}</td>
						<td class="n">{formatCount(run.new_messages)}</td>
						<td class="n">—</td>
						<td class="n">{formatCount(run.new_attachments)}</td>
						<td>ok</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.wrap {
		height: 100%;
		overflow-y: auto;
		max-width: 820px;
		margin: 0 auto;
		padding: 20px 18px 60px;
	}

	h1 {
		font-size: 20px;
		margin: 0 0 6px;
	}

	.dbline {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		padding: 8px 12px;
		margin-bottom: 14px;
		font-size: 13px;
	}

	.dbline code {
		flex: 1;
		min-width: 0;
		color: var(--text);
		word-break: break-all;
	}

	.dbline .muted {
		color: var(--muted);
	}

	h2 {
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--muted);
		margin: 26px 0 8px;
	}

	.lead {
		color: var(--muted);
		margin: 0 0 18px;
		max-width: 62ch;
	}

	.picker {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}

	.picker input {
		flex: 1;
		min-width: 220px;
	}

	.card {
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		padding: 10px 12px;
		margin-bottom: 8px;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}

	.card code {
		font-size: 12px;
		color: var(--muted);
		overflow-wrap: anywhere;
	}

	.card span {
		font-size: 13px;
		color: var(--muted);
	}

	.result ul {
		margin: 4px 0 0;
		padding-left: 18px;
		font-size: 13px;
		color: var(--muted);
	}

	.result b {
		color: var(--text);
	}

	.go {
		border-color: var(--accent);
		color: var(--accent);
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}

	th,
	td {
		text-align: left;
		padding: 6px 8px;
		border-bottom: 1px solid var(--line);
	}

	th {
		color: var(--muted);
		font-weight: 500;
		font-size: 11px;
		text-transform: uppercase;
	}

	.n {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	tr.sync td {
		color: var(--muted);
	}

	.bad {
		color: #ff8a8a;
	}

	.note {
		padding: 10px 0;
	}

	.error {
		color: #ff8a8a;
	}
</style>
