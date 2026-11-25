<script>
	import { api } from '$lib/api.js';
	import { base } from '$app/paths';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onMount, onDestroy } from 'svelte';
	import { formatRelativeDate, formatTimeRemaining } from '$lib/utils.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import DeleteDeviceDialog from '$lib/components/DeleteDeviceDialog.svelte';
	import OnboardingSessions from '$lib/components/OnboardingSessions.svelte';
	import BulkEditDialog from '$lib/components/BulkEditDialog.svelte';
	import { selectable } from '$lib/bulk.js';
	import { openPairing } from '$lib/stores/pairing.svelte.js';
	import { deviceUserName, deviceUserNames } from '$lib/device-user.js';
	import { deviceIcon, appSummary, platformLabel, haAccessOf, accessLabel, remoteState } from '$lib/device-info.js';
	import { KEY_PLATFORMS, controlPathOf } from '$lib/profiles/catalog.svelte.js';

	/** A TV or watch whose profile is "Hub only": it runs on "Checked", because it never runs without a key. */
	function keyPlatformOnHub(c, a) {
		const p = profilesById[c.profile_id];
		return a.level === 'checked' && KEY_PLATFORMS.includes(c.platform) && !!p && controlPathOf(p, { guest: c.access === 'guest' }) === 'hub';
	}
	import { t } from '$lib/i18n/index.svelte.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Avatar from '$lib/ui/Avatar.svelte';
	import StatusBadge from '$lib/ui/StatusBadge.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';

	const GROUP_KEY = 'hub.devices.group';
	let grouping = $state('owner');
	let isPhone = $state(false);

	let clients = $state([]);
	// Profiles, to name why a TV or watch on a "Hub only" profile is on "Checked".
	let profilesById = $state({});
	let loading = $state(true);
	let errorMsg = $state('');

	// Kick confirm + busy state
	let confirmKickOpen = $state(false);
	let confirmKickTitle = $state('');
	let confirmKickMessage = $state('');
	let confirmKickLabel = $state('');
	let pendingKick = $state(null);
	let kickingId = $state(null);
	// Member devices: removal dialog with the offline choice
	let deleteOpen = $state(false);
	let deleteTarget = $state(null);

	// Selection for bulk changes: ids, kept across search and reloads.
	let selected = $state(new Set());
	let bulkOpen = $state(false);

	// WS broadcast unsubscribers
	let unsubs = [];

	onMount(() => {
		try {
			grouping = localStorage.getItem(GROUP_KEY) === 'flat' ? 'flat' : 'owner';
		} catch {
			/* ignore */
		}
		const mq = window.matchMedia('(max-width: 767px)');
		const onMq = () => (isPhone = mq.matches);
		onMq();
		mq.addEventListener('change', onMq);
		unsubs.push(() => mq.removeEventListener('change', onMq));
		loadClients();
		api.listProfiles().then((r) => (profilesById = Object.fromEntries((r?.profiles || []).map((p) => [p.id, p])))).catch(() => {});

		unsubs.push(
			onBroadcast('client_paired', () => loadClients()),
			onBroadcast('client_connected', () => loadClients()),
			onBroadcast('client_disconnected', () => loadClients()),
			onBroadcast('client_status_updated', () => loadClients()),
			onBroadcast('client_kicked', () => loadClients()),
			onBroadcast('guests_expired', () => loadClients()),
			onBroadcast('guest_extended', () => loadClients()),
			onBroadcast('client_config_updated', () => loadClients()),
			onBroadcast('profiles_changed', () => loadClients()),
			onBroadcast('resync', () => loadClients()),
		);
	});

	onDestroy(() => {
		unsubs.forEach(fn => fn());
	});

	async function loadClients() {
		loading = clients.length === 0 && !errorMsg;
		try {
			clients = await api.getPairedClients();
			errorMsg = '';
			pruneSelection();
		} catch (e) {
			errorMsg = t('devices.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	function kickClient(client) {
		if (!client.pending_deletion && !isGuest(client)) {
			deleteTarget = client;
			deleteOpen = true;
			return;
		}
		pendingKick = client;
		if (client.pending_deletion) {
			// Already pending: the action just hides it from the list (background tombstone).
			confirmKickTitle = t('devices.confirmListTitle');
			confirmKickMessage = t('devices.confirmListMessage');
			confirmKickLabel = t('devices.removeFromList');
		} else if (isGuest(client)) {
			confirmKickTitle = t('devices.confirmGuestTitle');
			confirmKickMessage = t('devices.confirmGuestMessage', { name: client.name });
			confirmKickLabel = t('common.remove');
		} else {
			confirmKickTitle = t('devices.confirmDeleteTitle');
			confirmKickMessage = t('devices.confirmDeleteMessage', { name: client.name });
			confirmKickLabel = t('common.remove');
		}
		confirmKickOpen = true;
	}

	function pruneSelection() {
		const ok = new Set(clients.filter(selectable).map((c) => c.id));
		if ([...selected].some((id) => !ok.has(id))) selected = new Set([...selected].filter((id) => ok.has(id)));
	}

	function toggleSelected(id, on) {
		const next = new Set(selected);
		if (on) next.add(id);
		else next.delete(id);
		selected = next;
	}

	function toggleAllShown(on) {
		const next = new Set(selected);
		for (const c of shownSelectable) {
			if (on) next.add(c.id);
			else next.delete(c.id);
		}
		selected = next;
	}

	function openBulk() {
		bulkOpen = true;
	}

	function bulkDone(result) {
		addToast(t('bulk.resultToast', { count: result.changed }), result.changed ? 'success' : 'info');
		selected = new Set();
		loadClients();
	}

	function isGuest(c) {
		return c?.access === 'guest';
	}

	function setGrouping(v) {
		grouping = v;
		try {
			localStorage.setItem(GROUP_KEY, v);
		} catch {
			/* ignore */
		}
	}

	function roleOf(c) {
		return isGuest(c) ? 'guest' : c.owner_label ? 'member' : 'shared';
	}

	async function doDelete(removal) {
		const client = deleteTarget;
		if (!client) return;
		kickingId = client.id;
		try {
			// The device is signalled to reset; the row stays as "Waiting for device".
			await api.deleteClient(client.id, removal);
			const key = removal === 'immediate' ? 'devices.deletedImmediate' : removal === 'on_reconnect' ? 'devices.deletedOnReconnect' : 'devices.deleted';
			addToast(t(key, { name: client.name }), 'success');
			await loadClients();
		} catch (err) {
			addToast(t('devices.removeFailed', { message: err.message }), 'error');
		} finally {
			kickingId = null;
		}
	}

	async function doRevoke() {
		const client = deleteTarget;
		if (!client) return;
		kickingId = client.id;
		try {
			await api.revokeClient(client.id);
			addToast(t('devices.revokedToast', { name: client.name }), 'success');
			await loadClients();
		} catch (err) {
			addToast(t('devices.revokeFailed', { message: err.message }), 'error');
		} finally {
			kickingId = null;
		}
	}

	async function doKick() {
		const client = pendingKick;
		if (!client) return;
		kickingId = client.id;
		try {
			if (client.pending_deletion) {
				await api.dismissPending(client.id);
				addToast(t('devices.listRemoved', { name: client.name }), 'success');
			} else if (isGuest(client)) {
				// Guests are removed immediately (hard kick + delete).
				await api.kickClient(client.id);
				addToast(t('devices.guestDeleted', { name: client.name }), 'success');
			} else {
				// Regular clients enter the pending-deletion flow: the device is
				// signalled to reset; the row stays as "Waiting for device".
				await api.deleteClient(client.id);
				addToast(t('devices.deleted', { name: client.name }), 'success');
			}
			await loadClients();
		} catch (err) {
			addToast(t('devices.removeFailed', { message: err.message }), 'error');
		} finally {
			kickingId = null;
			pendingKick = null;
		}
	}

	// Live search: matches by client name, owner, client id, or the HA token name
	// (<namespace>:<client-id>[/<rand>]) — which contains the client id as a substring.
	let searchQuery = $state('');
	let filteredClients = $derived.by(() => {
		const q = searchQuery.trim().toLowerCase();
		if (!q) return clients;
		return clients.filter(c => {
			const name = (c.name || '').toLowerCase();
			const owner = (c.owner_label || '').toLowerCase();
			const id = (c.id || '').toLowerCase();
			const user = deviceUserNames(c).some((u) => u.toLowerCase().includes(q));
			return name.includes(q) || owner.includes(q) || user || id.includes(q) || (id && q.includes(id));
		});
	});

	// Sorted: devices of the same owner together (no owner last), members
	// before guests, connected first, newest first.
	let sortedClients = $derived(filteredClients.toSorted((a, b) => {
		const oa = (a.owner_label || '').toLowerCase();
		const ob = (b.owner_label || '').toLowerCase();
		if (grouping === 'owner' && oa !== ob) return !oa ? 1 : !ob ? -1 : oa.localeCompare(ob);
		if (isGuest(a) !== isGuest(b)) return isGuest(a) ? 1 : -1;
		if (a.is_connected !== b.is_connected) return a.is_connected ? -1 : 1;
		return new Date(b.created_at) - new Date(a.created_at);
	}));

	/**
	 * A child's watch: a watch with parental
	 * controls that follows the phone it was set up with. Listed right under
	 * that phone (when the phone is listed).
	 */
	function childWatchPhone(c, shown) {
		if (!['watchos', 'wearos'].includes(c?.platform) || !c.child_protection) return null;
		const id = c.set_up_by?.client_id;
		return (id && shown.find((p) => p.id === id && p.id !== c.id)) || null;
	}

	/** Rows with group headers when grouped by owner; a child's watch under its phone. */
	let rows = $derived.by(() => {
		const out = [];
		let last = null;
		const under = new Map();
		for (const c of sortedClients) {
			const phone = childWatchPhone(c, sortedClients);
			if (phone) under.set(phone.id, [...(under.get(phone.id) || []), c]);
		}
		const placed = new Set([...under.values()].flat().map((c) => c.id));
		for (const c of sortedClients) {
			if (placed.has(c.id)) continue;
			if (grouping === 'owner') {
				const g = (c.owner_label || '').trim();
				if (g !== last) {
					out.push({ group: g || t('devices.household'), key: `g:${g}` });
					last = g;
				}
			}
			out.push({ client: c, key: c.id });
			for (const w of under.get(c.id) || []) out.push({ client: w, key: w.id, watchOf: c.name });
		}
		return out;
	});

	let shownSelectable = $derived(sortedClients.filter(selectable));
	let allShownSelected = $derived(shownSelectable.length > 0 && shownSelectable.every((c) => selected.has(c.id)));
	let someShownSelected = $derived(shownSelectable.some((c) => selected.has(c.id)));
	let selectedClients = $derived(clients.filter((c) => selected.has(c.id)));

	let connectedCount = $derived(clients.filter(c => c.is_connected && !c.pending_deletion).length);
	let activeCount = $derived(clients.filter(c => !c.pending_deletion).length);

	function menuItems(c) {
		return [
			{ label: t('devices.open', { name: c.name }), icon: 'chev', onclick: () => (location.href = `${base}/clients/${c.id}`) },
			{ separator: true },
			{
				label: c.pending_deletion ? t('devices.removeFromList') : isGuest(c) ? t('devices.removeGuest') : t('devices.remove'),
				icon: 'trash',
				danger: true,
				disabled: kickingId === c.id,
				onclick: () => kickClient(c),
			},
		];
	}
</script>

{#snippet roleBadge(c)}
	{#if c.pending_deletion}
		<span class="badge badge-err" title={t('devices.waitingInfo')}>{t('devices.waiting')}</span>
	{:else if isGuest(c)}
		{@const left = formatTimeRemaining(c.expires_at)}
		<span class="badge badge-guest">{c.expires_at ? t('devices.guestLeft', { time: left }) : t('devices.roles.guest')}</span>
	{:else if !c.is_active}
		<span class="badge badge-err">{t('devices.revoked')}</span>
	{:else}
		<span class="badge">{t(`devices.roles.${roleOf(c)}`)}</span>
	{/if}
	{#if c.profile_name}
		<span class="badge {c.child_protection ? 'badge-accent' : ''}" data-testid="profile-badge"><Icon name={c.child_protection ? 'child' : 'template'} size="13" />{c.profile_name}</span>
	{/if}
	{#if c.override_count}
		<span class="badge badge-warn" data-testid="override-badge" title={t('profiles.adjustedCount', { count: c.override_count })}><Icon name="sliders" size="13" />{c.override_count}</span>
	{/if}
	{#if !c.pending_deletion && c.is_active && c.ha_token_status === 'pending'}
		<span class="badge badge-warn" data-testid="setup-badge" title={t('devices.settingUpTitle')}>{t('devices.settingUp')}</span>
	{/if}
	{#if !c.pending_deletion && c.is_active && c.update_required}
		<span class="badge badge-warn" data-testid="update-badge" title={t('devices.updateRequiredTitle')}>{t('devices.updateRequired')}</span>
	{/if}
{/snippet}

{#snippet selectBox(c)}
	<label class="sel" title={t('bulk.select', { name: c.name })}>
		<input type="checkbox" checked={selected.has(c.id)} disabled={!selectable(c)} onchange={(e) => toggleSelected(c.id, e.currentTarget.checked)} data-testid="select-{c.id}" />
		<span class="sr-only">{t('bulk.select', { name: c.name })}</span>
	</label>
{/snippet}

{#snippet appCell(c)}
	{#if c.app_version || c.platform}
		<span class="app" data-testid="app-{c.id}" title={c.device_model || ''}>{appSummary(c)}</span>
	{:else}
		<span class="t-muted">{t('devices.appUnknown')}</span>
	{/if}
{/snippet}

{#snippet accessCell(c)}
	{@const a = haAccessOf(c)}
	{#if a.level}
		<span class="acc" data-testid="ha-access-{c.id}" data-level={a.level} data-key={a.key}>
			<span class="lk on"><Icon name={a.level === 'hub' ? 'home' : a.level === 'unknown' ? 'warn' : 'key'} />{accessLabel(a.level)}</span>
			<small class="t-help">{t(`devices.key.${a.key}`)}</small>
			{#if keyPlatformOnHub(c, a)}<small class="t-help reason" data-testid="ha-access-reason-{c.id}" title={t('profiles.preview.platformKey')}>{t('devices.keyPlatform')}</small>{/if}
		</span>
	{:else}
		<span class="t-muted">–</span>
	{/if}
{/snippet}

{#snippet remoteCell(c)}
	{@const r = remoteState(c)}
	<span class="lk" class:on={r.id !== 'off'} data-testid="remote-{c.id}" data-state={r.id}><Icon name={r.icon} />{r.label}</span>
{/snippet}

<div class="page">
	<PageHeader
		title={t('devices.title')}
		subtitle={clients.length ? t('devices.subtitle', { devices: t('devices.count', { count: activeCount }), online: connectedCount }) : ''}
	>
		{#snippet actions()}
			<button type="button" class="btn btn-primary" onclick={() => openPairing('member')} data-testid="open-pairing"><Icon name="qr" />{t('pairing.open')}</button>
		{/snippet}
	</PageHeader>

	<OnboardingSessions />

	{#if clients.length > 0}
		<div class="toolbar">
			<label class="search">
				<Icon name="search" />
				<span class="sr-only">{t('devices.searchLabel')}</span>
				<input type="search" bind:value={searchQuery} placeholder={t('devices.searchPlaceholder')} />
			</label>
			<Segmented label={t('devices.grouping')} options={[{ value: 'owner', label: t('devices.byOwner') }, { value: 'flat', label: t('devices.flat') }]} value={grouping} onchange={setGrouping} />
		</div>
		{#if searchQuery.trim()}<p class="t-help mb-2" aria-live="polite">{t('devices.matches', { shown: sortedClients.length, total: clients.length })}</p>{/if}
	{/if}

	{#if loading}
		<LoadingState />
	{:else if errorMsg && clients.length === 0}
		<div class="card"><ErrorState message={errorMsg} onretry={loadClients} /></div>
	{:else if clients.length === 0}
		<div class="card">
			<EmptyState icon="iphone" title={t('devices.emptyTitle')} text={t('devices.emptyText')}>
				<button type="button" class="btn btn-primary" onclick={() => openPairing('member')}><Icon name="qr" />{t('pairing.open')}</button>
			</EmptyState>
		</div>
	{:else if sortedClients.length === 0}
		<div class="card">
			<EmptyState compact icon="search" title={t('devices.noMatch', { query: searchQuery })}>
				<button type="button" class="btn" onclick={() => (searchQuery = '')}>{t('devices.clearSearch')}</button>
			</EmptyState>
		</div>
	{:else if isPhone}
		<label class="check mb-1 selall">
			<input type="checkbox" checked={allShownSelected} indeterminate={someShownSelected && !allShownSelected} onchange={(e) => toggleAllShown(e.currentTarget.checked)} data-testid="select-all" />
			{t('bulk.selectAllShort')}
		</label>
		<div class="cards" data-testid="device-cards">
			{#each rows as row (row.key)}
				{#if row.group}
					<h2 class="t-overline ghd">{row.group}</h2>
				{:else}
					{@const c = row.client}
					<article class="card dcard" class:muted={c.pending_deletion} class:picked={selected.has(c.id)}>
						<div class="dh">
							{@render selectBox(c)}
							<Icon name={deviceIcon(c)} label={platformLabel(c.platform) || undefined} />
							<a class="dname" href="{base}/clients/{c.id}">{c.name}</a>
							<StatusBadge online={c.is_connected} />
						</div>
						{#if row.watchOf}<p class="t-help watchof" data-testid="watch-of-{c.id}"><Icon name="child" size="13" />{t('devices.childWatchOf', { name: row.watchOf })}</p>{/if}
						<div class="meta">
							{@render roleBadge(c)}
							{#if c.owner_label}<span data-testid="owner-badge">{c.owner_label}</span>{/if}
							<span>{c.is_connected ? t('common.online') : formatRelativeDate(c.last_seen)}</span>
							{#if c.app_version || c.platform}<span class="t-help">{appSummary(c)}</span>{/if}
						</div>
						<div class="row justify-between">
							<span class="t-label">{t('devices.remote')}</span>
							{@render remoteCell(c)}
						</div>
						<div class="row justify-between">
							<span class="t-label">{t('devices.col.haAccess')}</span>
							{@render accessCell(c)}
						</div>
					</article>
				{/if}
			{/each}
		</div>
	{:else}
		<div class="table-wrap">
			<table class="table" data-testid="device-table">
				<thead>
					<tr>
						<th scope="col" class="c-sel">
							<label class="sel">
								<input type="checkbox" checked={allShownSelected} indeterminate={someShownSelected && !allShownSelected} onchange={(e) => toggleAllShown(e.currentTarget.checked)} data-testid="select-all" />
								<span class="sr-only">{t('bulk.selectAll')}</span>
							</label>
						</th>
						<th scope="col">{t('devices.col.device')}</th>
						<th scope="col">{t('devices.col.owner')}</th>
						<th scope="col">{t('devices.col.role')}</th>
						<th scope="col">{t('devices.col.status')}</th>
						<th scope="col">{t('devices.col.remote')}</th>
						<th scope="col">{t('devices.col.haAccess')}</th>
						<th scope="col" class="c-opt">{t('devices.appCol')}</th>
						<th scope="col" class="c-opt">{t('devices.col.seen')}</th>
						<th scope="col"><span class="sr-only">{t('common.more')}</span></th>
					</tr>
				</thead>
				<tbody>
					{#each rows as row (row.key)}
						{#if row.group}
							<tr class="group"><th scope="rowgroup" colspan="10">{row.group}</th></tr>
						{:else}
							{@const c = row.client}
							<tr class:muted={c.pending_deletion} class:picked={selected.has(c.id)} class:under={row.watchOf}>
								<td class="c-sel">{@render selectBox(c)}</td>
								<td>
									<div class="dn">
										<Icon name={deviceIcon(c)} label={platformLabel(c.platform) || undefined} />
										<span class="min-w-0">
											<a class="dname" href="{base}/clients/{c.id}">{c.name}</a>
											<small class="t-mono" title={deviceUserName(c)}>{deviceUserName(c)}</small>
											{#if row.watchOf}<small class="watchof" data-testid="watch-of-{c.id}"><Icon name="child" size="13" />{t('devices.childWatchOf', { name: row.watchOf })}</small>{/if}
										</span>
									</div>
								</td>
								<td>
									<span class="owner">
										<Avatar name={c.owner_label} />
										{#if c.owner_label}<span data-testid="owner-badge">{c.owner_label}</span>{:else}<span class="t-muted">–</span>{/if}
									</span>
								</td>
								<td><div class="row gap-1">{@render roleBadge(c)}</div></td>
								<td><StatusBadge online={c.is_connected} /></td>
								<td>{@render remoteCell(c)}</td>
								<td>{@render accessCell(c)}</td>
								<td class="c-opt t-help">{@render appCell(c)}</td>
								<td class="c-opt t-help">{c.is_connected ? t('common.online') : formatRelativeDate(c.last_seen)}</td>
								<td class="text-right">
									<Menu iconOnly ariaLabel={t('devices.actionsFor', { name: c.name })} items={menuItems(c)} />
								</td>
							</tr>
						{/if}
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
	{#if selected.size > 0}
		<div class="selbar" role="region" aria-label={t('bulk.barLabel')} data-testid="bulk-bar">
			<span class="selcount" aria-live="polite">{t('bulk.selected', { count: selected.size })}</span>
			<div class="selbtns">
				<button type="button" class="btn" onclick={() => (selected = new Set())} data-testid="bulk-clear">{t('bulk.clear')}</button>
				<button type="button" class="btn btn-primary" onclick={openBulk} data-testid="bulk-open"><Icon name="edit" />{t('bulk.edit')}</button>
			</div>
		</div>
	{/if}
</div>

<BulkEditDialog bind:open={bulkOpen} clients={selectedClients} ondone={bulkDone} />
<DeleteDeviceDialog bind:open={deleteOpen} client={deleteTarget} busy={!!kickingId} onremove={doDelete} onrevoke={doRevoke} />
<ConfirmDialog bind:open={confirmKickOpen} title={confirmKickTitle} message={confirmKickMessage} confirmLabel={confirmKickLabel} danger onconfirm={doKick} />

<style>
	.toolbar {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
		margin-bottom: 12px;
	}
	.search {
		flex: 1;
		min-width: 220px;
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: var(--h-ctl);
		padding: 0 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-ctl);
		background: var(--surface);
		color: var(--ink-3);
	}
	.search:focus-within {
		outline: 2px solid var(--focus);
	}
	.search input {
		flex: 1;
		min-width: 0;
		border: 0;
		background: none;
		color: var(--ink);
		font: inherit;
		outline: none;
		min-height: calc(var(--h-ctl) - 2px);
	}
	.search input::placeholder {
		color: var(--ink-3);
	}
	.dn {
		display: flex;
		gap: 10px;
		align-items: center;
	}
	.dn :global(.ic) {
		color: var(--ink-2);
		width: 20px;
		height: 20px;
	}
	.dn small {
		display: block;
		color: var(--ink-3);
		max-width: 260px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dname {
		font-weight: 650;
		color: var(--ink);
		text-decoration: none;
	}
	.dname:hover {
		text-decoration: underline;
	}
	.app {
		white-space: nowrap;
	}
	.owner {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.lk {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--ink-3);
		font-size: var(--fs-cap);
	}
	.lk.on {
		color: var(--ink-2);
	}
	.lk :global(.ic) {
		width: 13px;
		height: 13px;
	}
	.acc {
		display: inline-grid;
		gap: 1px;
		justify-items: start;
	}
	.acc small {
		font-size: var(--fs-cap);
		white-space: nowrap;
	}
	.acc small.reason {
		white-space: normal;
		max-width: 11rem;
	}
	tr.muted td,
	.dcard.muted {
		opacity: 0.7;
	}
	.table :global(tr.group th) {
		background: var(--surface-2);
		font-weight: 700;
		font-size: 12.5px;
		color: var(--ink-2);
		padding: 7px 12px;
		text-align: left;
		text-transform: none;
		letter-spacing: 0;
	}
	@media (max-width: 1100px) {
		.c-opt {
			display: none;
		}
	}
	.ghd {
		margin: 14px 4px 8px;
	}
	.c-sel {
		width: 44px;
		padding-right: 0;
	}
	.sel {
		display: inline-grid;
		place-items: center;
		min-width: 32px;
		min-height: 32px;
		cursor: pointer;
	}
	.sel input,
	.selall input {
		width: 18px;
		height: 18px;
		accent-color: var(--ink);
	}
	tr.picked td {
		background: var(--fill-1, var(--surface-2));
	}
	.dcard.picked {
		outline: 2px solid var(--ink-3);
	}
	/* Selection bar: count + two equally wide buttons. */
	.selbar {
		position: sticky;
		bottom: 12px;
		z-index: 5;
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		justify-content: space-between;
		margin-top: 12px;
		padding: 10px 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		box-shadow: 0 6px 24px rgb(0 0 0 / 0.12);
	}
	.selcount {
		font-weight: 650;
	}
	.selbtns {
		display: inline-grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(10rem, 1fr);
		gap: 8px;
	}
	@media (max-width: 767px) {
		.sel {
			min-width: 44px;
			min-height: 44px;
			margin: -8px 0 -8px -8px;
		}
		.selall {
			min-height: 44px;
		}
		.selbar {
			bottom: calc(8px + env(safe-area-inset-bottom));
		}
		.selbtns {
			width: 100%;
			grid-auto-columns: minmax(0, 1fr);
		}
		.selbtns :global(.ic) {
			display: none;
		}
	}
	.dcard {
		display: grid;
		gap: 8px;
		margin-bottom: 10px;
		padding: 12px 14px;
	}
	.dh {
		display: flex;
		gap: 10px;
		align-items: center;
	}
	.dh .dname {
		flex: 1;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		gap: 6px 12px;
		font-size: var(--fs-sm);
		color: var(--ink-2);
		align-items: center;
	}
	.watchof {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	tr.under td:nth-child(2) {
		padding-left: 28px;
	}
</style>
