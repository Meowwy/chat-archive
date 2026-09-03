<script>
	import { api } from '$lib/api.js';
	import { formatCount, platformColor, platformLabel } from '$lib/format.js';

	let data = $state(null);
	let busy = $state(false);
	let message = $state(null);
	let error = $state(null);

	let selected = $state([]); // identity ids, as strings
	let filter = $state('');
	let onlyUnmapped = $state(false);
	let newName = $state('');
	let target = $state('');
	let renaming = $state(null); // person_id being renamed
	let renameTo = $state('');
	let confirmDelete = $state(null);

	async function load() {
		try {
			data = await api.people();
			error = null;
		} catch (e) {
			error = e.message;
		}
	}

	$effect(() => {
		load();
	});

	/** Run a mutation, then reload - the server is the source of truth. */
	async function run(label, action) {
		busy = true;
		message = null;
		error = null;
		try {
			const result = await action();
			message = typeof label === 'function' ? label(result) : label;
			await load();
		} catch (e) {
			error = e.message;
		} finally {
			busy = false;
		}
	}

	let people = $derived(data?.people ?? []);
	let allIdentities = $derived(data?.identities ?? []);

	let byPerson = $derived.by(() => {
		const map = new Map();
		for (const row of allIdentities) {
			if (row.person_id === null) continue;
			if (!map.has(row.person_id)) map.set(row.person_id, []);
			map.get(row.person_id).push(row);
		}
		return map;
	});

	let visible = $derived.by(() => {
		const needle = filter.trim().toLowerCase();
		return allIdentities.filter((row) => {
			if (onlyUnmapped && row.person_id !== null) return false;
			if (!needle) return true;
			return `${row.name} ${row.display_name ?? ''} ${row.person ?? ''}`
				.toLowerCase()
				.includes(needle);
		});
	});

	let mapped = $derived(allIdentities.filter((row) => row.person_id !== null).length);
	let picked = $derived(allIdentities.filter((row) => selected.includes(row.id)));
	// Name the new person after whoever of the selection sent the most.
	let suggestion = $derived(picked[0] ? picked[0].display_name || picked[0].name : '');
	let allVisiblePicked = $derived(
		visible.length > 0 && visible.every((row) => selected.includes(row.id))
	);

	const nameOf = (row) => row.display_name || row.name;

	function toggle(id) {
		selected = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
	}

	function toggleAllVisible() {
		selected = allVisiblePicked
			? selected.filter((id) => !visible.some((row) => row.id === id))
			: [...new Set([...selected, ...visible.map((row) => row.id)])];
	}

	function createPerson() {
		const display = newName.trim() || suggestion;
		if (!display) return;
		const ids = [...selected];
		run(
			(r) => `Person “${display}” created, ${formatCount(r.linked)} identities linked.`,
			async () => {
				const result = await api.createPerson({ display, user_ids: ids });
				selected = [];
				newName = '';
				return result;
			}
		);
	}

	function addToPerson() {
		if (!target || selected.length === 0) return;
		const person = people.find((p) => p.person_id === target);
		const ids = [...selected];
		run(
			(r) => `${formatCount(r.linked)} identities added to “${person?.display ?? ''}”.`,
			async () => {
				const result = await api.linkIdentities(target, ids);
				selected = [];
				return result;
			}
		);
	}

	function unlinkSelected() {
		const ids = [...selected];
		run(
			(r) => `${formatCount(r.linked)} identities detached.`,
			async () => {
				const result = await api.linkIdentities(null, ids);
				selected = [];
				return result;
			}
		);
	}

	function unlinkOne(row) {
		run('Identity detached.', () => api.linkIdentities(null, [row.id]));
	}

	function saveRename(person) {
		const display = renameTo.trim();
		if (!display || display === person.display) {
			renaming = null;
			return;
		}
		run(`Renamed to “${display}”.`, async () => {
			const result = await api.updatePerson(person.person_id, { display });
			renaming = null;
			return result;
		});
	}

	function setSelf(person, value) {
		run(value ? `“${person.display}” is you.` : 'No longer marked as you.', () =>
			api.updatePerson(person.person_id, { is_self: value })
		);
	}

	function remove(person) {
		confirmDelete = null;
		run(
			(r) => `Person deleted, ${formatCount(r.unlinked)} identities detached.`,
			() => api.deletePerson(person.person_id)
		);
	}
</script>

<div class="wrap">
	<h1>People across platforms</h1>
	<p class="lead">
		Discord has stable numeric ids; Facebook and Instagram exports carry only a display name.
		Tick the identities that belong to one person, link them and give them a name — that name is
		then used everywhere in the app in place of the per-platform ones.
	</p>

	{#if message}<p class="ok">{message}</p>{/if}
	{#if error}<p class="err">{error}</p>{/if}

	{#if data}
		<div class="summary">
			<span><b>{formatCount(allIdentities.length)}</b> identities</span>
			<span><b>{formatCount(mapped)}</b> linked</span>
			<span><b>{formatCount(people.length)}</b> people</span>
		</div>

		<h2>People</h2>
		{#if people.length === 0}
			<p class="muted empty">
				No people yet. Tick the identities below that belong to one person and create them.
			</p>
		{:else}
			<div class="cards">
				{#each people as person (person.person_id)}
					<div class="card" class:self={person.is_self}>
						<div class="card-head">
							{#if renaming === person.person_id}
								<input
									class="rename"
									bind:value={renameTo}
									{@attach (node) => node.focus()}
									onkeydown={(e) => {
										if (e.key === 'Enter') saveRename(person);
										if (e.key === 'Escape') renaming = null;
									}}
								/>
								<button class="tiny" onclick={() => saveRename(person)}>Save</button>
								<button class="tiny ghost" onclick={() => (renaming = null)}>Cancel</button>
							{:else}
								<b class="pname">{person.display}</b>
								{#if person.is_self}<span class="badge">me</span>{/if}
								<button
									class="tiny ghost"
									onclick={() => {
										renaming = person.person_id;
										renameTo = person.display;
									}}>Rename</button
								>
							{/if}
						</div>

						<p class="card-sub">
							{formatCount(person.identities)} identities · {formatCount(person.messages)} messages
						</p>

						<div class="chips">
							{#each byPerson.get(person.person_id) ?? [] as row (row.id)}
								<span class="chip" style:--c={platformColor(row.platform)}>
									<span class="dot"></span>
									<span class="cname" title="{platformLabel(row.platform)}: {nameOf(row)}">
										{nameOf(row)}
									</span>
									<button
										class="x"
										title="Detach this identity"
										disabled={busy}
										onclick={() => unlinkOne(row)}>×</button
									>
								</span>
							{:else}
								<span class="muted small">no identities — pick some in the table below</span>
							{/each}
						</div>

						<div class="card-foot">
							<label class="selfbox">
								<input
									type="checkbox"
									checked={Boolean(person.is_self)}
									disabled={busy}
									onchange={(e) => setSelf(person, e.currentTarget.checked)}
								/>
								this is me
							</label>
							{#if confirmDelete === person.person_id}
								<button class="tiny danger" onclick={() => remove(person)}>Really delete</button>
								<button class="tiny ghost" onclick={() => (confirmDelete = null)}>No</button>
							{:else}
								<button class="tiny ghost" onclick={() => (confirmDelete = person.person_id)}>
									Delete
								</button>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}

		<h2>Identities in the archive</h2>
		<div class="filters">
			<input class="search" placeholder="Search names…" bind:value={filter} />
			<label>
				<input type="checkbox" bind:checked={onlyUnmapped} />
				only unlinked
			</label>
			<button class="tiny ghost" onclick={toggleAllVisible} disabled={visible.length === 0}>
				{allVisiblePicked ? 'Clear selection' : 'Select all'}
			</button>
		</div>

		<table>
			<thead>
				<tr>
					<th class="c"></th>
					<th>Platform</th>
					<th>Name in the export</th>
					<th class="n">Messages</th>
					<th>Person</th>
				</tr>
			</thead>
			<tbody>
				{#each visible as row (row.id)}
					<tr class:picked={selected.includes(row.id)}>
						<td class="c">
							<input
								id="cb{row.id}"
								type="checkbox"
								checked={selected.includes(row.id)}
								onchange={() => toggle(row.id)}
							/>
						</td>
						<td class="plat">
							<span class="dot" style:--c={platformColor(row.platform)}></span>
							{platformLabel(row.platform)}
						</td>
						<td><label for="cb{row.id}">{nameOf(row)}</label></td>
						<td class="n">{formatCount(row.messages)}</td>
						<td>
							{#if row.person}
								{row.person}
							{:else}
								<span class="muted">unlinked</span>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>

		{#if visible.length === 0}
			<p class="muted empty">Nothing matches the filter.</p>
		{/if}
	{:else if !error}
		<p class="muted">Loading…</p>
	{/if}
</div>

{#if selected.length > 0}
	<div class="tray">
		<b>{formatCount(selected.length)} selected</b>
		<input
			class="name"
			placeholder={suggestion || 'Person name'}
			bind:value={newName}
			onkeydown={(e) => e.key === 'Enter' && createPerson()}
		/>
		<button class="primary" onclick={createPerson} disabled={busy}>Create person</button>
		<span class="sep">or</span>
		<select bind:value={target}>
			<option value="">add to a person…</option>
			{#each people as person (person.person_id)}
				<option value={person.person_id}>{person.display}</option>
			{/each}
		</select>
		<button onclick={addToPerson} disabled={busy || !target}>Add</button>
		<button class="ghost" onclick={unlinkSelected} disabled={busy}>Detach</button>
		<button class="ghost" onclick={() => (selected = [])}>Clear selection</button>
	</div>
{/if}

<style>
	.wrap {
		height: 100%;
		overflow-y: auto;
		max-width: 880px;
		margin: 0 auto;
		padding: 20px 18px 110px;
	}

	h1 {
		font-size: 20px;
		margin: 0 0 6px;
	}

	h2 {
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--muted);
		margin: 26px 0 10px;
	}

	.lead {
		color: var(--muted);
		margin: 0 0 18px;
		max-width: 68ch;
	}

	.summary {
		display: flex;
		gap: 16px;
		flex-wrap: wrap;
		font-size: 13px;
		color: var(--muted);
	}

	.summary b {
		color: var(--text);
	}

	.cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 10px;
	}

	.card {
		border: 1px solid var(--line);
		border-radius: var(--radius);
		background: var(--panel);
		padding: 10px 12px;
	}

	.card.self {
		border-color: var(--accent);
	}

	.card-head {
		display: flex;
		gap: 6px;
		align-items: center;
		flex-wrap: wrap;
	}

	.pname {
		font-size: 14px;
		overflow-wrap: anywhere;
	}

	.badge {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		background: var(--accent);
		color: #fff;
		border-radius: 999px;
		padding: 1px 7px;
	}

	.card-sub {
		margin: 3px 0 8px;
		font-size: 12px;
		color: var(--muted);
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		max-width: 100%;
		background: var(--panel-2);
		border-radius: 999px;
		padding: 2px 4px 2px 8px;
		font-size: 12px;
	}

	.cname {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 150px;
	}

	.x {
		border: none;
		background: none;
		color: var(--muted);
		cursor: pointer;
		font-size: 14px;
		line-height: 1;
		padding: 0 4px;
		border-radius: 50%;
	}

	.x:hover {
		color: #ff8a8a;
	}

	.card-foot {
		display: flex;
		gap: 8px;
		align-items: center;
		margin-top: 10px;
		font-size: 12px;
	}

	.selfbox {
		display: flex;
		gap: 5px;
		align-items: center;
		color: var(--muted);
		cursor: pointer;
		margin-right: auto;
	}

	.tiny {
		font-size: 11px;
		padding: 3px 8px;
	}

	.ghost {
		background: none;
		border: 1px solid var(--line);
		color: var(--muted);
	}

	.ghost:hover:not(:disabled) {
		color: var(--text);
	}

	.danger {
		background: #a33;
		border-color: #a33;
		color: #fff;
	}

	.rename {
		flex: 1;
		min-width: 120px;
	}

	.filters {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
		margin-bottom: 8px;
		font-size: 13px;
		color: var(--muted);
	}

	.filters .search {
		flex: 1;
		min-width: 160px;
	}

	.filters label {
		display: flex;
		gap: 5px;
		align-items: center;
		cursor: pointer;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 13px;
	}

	th,
	td {
		text-align: left;
		padding: 5px 8px;
		border-bottom: 1px solid var(--line);
	}

	th {
		color: var(--muted);
		font-weight: 500;
		font-size: 11px;
		text-transform: uppercase;
	}

	tr.picked {
		background: var(--panel);
	}

	td label {
		cursor: pointer;
		display: block;
	}

	.c {
		width: 26px;
	}

	.n {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.plat {
		color: var(--muted);
		white-space: nowrap;
	}

	.dot {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--c);
	}

	.tray {
		position: fixed;
		left: 50%;
		bottom: 16px;
		transform: translateX(-50%);
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		max-width: min(94vw, 900px);
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35);
		padding: 10px 14px;
		z-index: 15;
		font-size: 13px;
	}

	.tray .name {
		min-width: 170px;
	}

	.tray .sep {
		color: var(--muted);
		font-size: 12px;
	}

	.primary {
		background: var(--accent);
		border-color: var(--accent);
		color: #fff;
	}

	.muted {
		color: var(--muted);
	}

	.small {
		font-size: 12px;
	}

	.empty {
		font-size: 13px;
		margin: 4px 0 0;
	}

	.ok {
		color: #6fd08c;
	}

	.err {
		color: #ff8a8a;
	}
</style>
