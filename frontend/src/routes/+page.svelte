<script>
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { openPairing } from '$lib/stores/pairing.svelte.js';
	import { isSetupSlow } from '$lib/device-user.js';
	import { shouldAutoOpen } from '$lib/setup.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { formatRelativeDate, formatTimeRemaining, debounce } from '$lib/utils.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import StatusBadge from '$lib/ui/StatusBadge.svelte';
	import Avatar from '$lib/ui/Avatar.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import SetupCard from '$lib/components/dashboard/SetupCard.svelte';
	import RecentImportant from '$lib/components/dashboard/RecentImportant.svelte';
	import HaCleartextHint from '$lib/components/HaCleartextHint.svelte';

	let areas = $state([]);
	let clients = $state([]);
	let notifications = $state([]);
	let integration = $state(null);
	let haUrls = $state(null);
	let guestProfile = $state(null);
	let setup = $state(null);
	let loading = $state(true);
	let loadError = $state('');
	let now = $state(Date.now());

	let unsubs = [];
	let clock;

	onMount(async () => {
		await loadAll();
		clock = setInterval(() => (now = Date.now()), 30_000);
		unsubs.push(
			onBroadcast('area_created', refreshAreas),
			onBroadcast('area_updated', refreshAreas),
			onBroadcast('area_deleted', refreshAreas),
			onBroadcast('areas_deleted', refreshAreas),
			onBroadcast('entity_added', refreshAreas),
			onBroadcast('entity_removed', refreshAreas),
			onBroadcast('client_connected', refreshClients),
			onBroadcast('client_disconnected', refreshClients),
			onBroadcast('client_paired', refreshClients),
			onBroadcast('client_kicked', refreshClients),
			onBroadcast('guests_expired', refreshClients),
			onBroadcast('guest_extended', refreshClients),
			onBroadcast('client_config_updated', refreshClients),
			onBroadcast('profiles_changed', refreshClients),
			onBroadcast('notification_created', refreshNotifications),
			onBroadcast('notification_delivered', refreshNotifications),
			onBroadcast('notification_deleted', refreshNotifications),
			onBroadcast('notifications_cleared', refreshNotifications),
			onBroadcast('resync', () => {
				refreshAreas();
				refreshClients();
				refreshNotifications();
			}),
			onBroadcast('client_status_updated', (data) => {
				clients = clients.map((c) => (c.id === data?.id ? { ...c, ...data } : c));
			}),
		);
	});

	const refreshAreas = debounce(async () => {
		try {
			areas = await api.getAreas();
		} catch {
			/* keep the last state; the next event retries */
		}
	}, 250);
	const refreshClients = debounce(async () => {
		try {
			clients = await api.getPairedClients();
		} catch {
			/* keep the last state */
		}
	}, 250);
	const refreshNotifications = debounce(async () => {
		try {
			notifications = await api.getNotifications('active');
		} catch {
			/* keep the last state */
		}
	}, 250);

	onDestroy(() => {
		clearInterval(clock);
		refreshAreas.cancel();
		refreshClients.cancel();
		refreshNotifications.cancel();
		unsubs.forEach((fn) => fn());
	});

	async function loadAll() {
		loadError = '';
		try {
			const [a, c] = await Promise.all([api.getAreas(), api.getPairedClients()]);
			areas = a;
			clients = c;
		} catch (e) {
			loadError = e.message;
			loading = false;
			return;
		}
		// Secondary data: a failure only hides the corresponding hint.
		const [n, i, g, s, h] = await Promise.allSettled([
			api.getNotifications('active'),
			api.integrationStatus(),
			Promise.all([api.getProfileSettings(), api.listProfiles()]),
			api.getSetupAssistant(),
			api.getHaUrls(),
		]);
		haUrls = h.status === 'fulfilled' ? h.value : null;
		notifications = n.status === 'fulfilled' && Array.isArray(n.value) ? n.value : [];
		integration = i.status === 'fulfilled' ? i.value : null;
		// Default guest profile: does it show any area?
		if (g.status === 'fulfilled') {
			const [ps, pl] = g.value;
			guestProfile = (pl?.profiles || []).find((p) => p.id === ps?.default_profiles?.guest) || null;
		}
		setup = s.status === 'fulfilled' ? s.value : null;
		// First visit of a fresh hub: the setup assistant opens by itself, once.
		if (shouldAutoOpen(setup, { areas, devices: clients })) {
			goto(`${base}/setup`, { replaceState: true });
			return;
		}
		loading = false;
	}

	let devices = $derived(clients.filter((c) => !c.pending_deletion));
	let online = $derived(devices.filter((c) => c.is_connected).length);
	let inactiveAreas = $derived(areas.filter((a) => !a.is_active).length);
	let guests = $derived(
		devices
			.filter((c) => c.access === 'guest' && (!c.expires_at || Date.parse(c.expires_at) > now))
			.toSorted((a, b) => Date.parse(a.expires_at || '9999') - Date.parse(b.expires_at || '9999')),
	);
	let lastNotification = $derived(
		notifications.toSorted((a, b) => Date.parse(b.created_at || 0) - Date.parse(a.created_at || 0))[0],
	);
	let slowSetup = $derived(devices.filter((c) => isSetupSlow(c, now)));
	let guestAreasDefined = $derived.by(() => {
		const a = guestProfile?.rules?.areas;
		return !!a && (a.mode === 'all' || a.own_area || (a.area_ids || []).length > 0 || (a.tags || []).length > 0);
	});

	let checklist = $derived([
		{ key: 'integration', done: !!integration?.connected, href: `${base}/settings?tab=system` },
		{ key: 'haUsers', done: devices.length > 0 && devices.every((c) => c.ha_token_status === 'active'), href: `${base}/clients` },
		{ key: 'area', done: areas.length > 0, href: `${base}/areas` },
		{ key: 'device', done: devices.length > 0, action: () => openPairing('member') },
		{ key: 'guestAreas', done: guestAreasDefined, href: guestProfile ? `${base}/profiles/${guestProfile.id}#r-areas` : `${base}/profiles` },
	]);
	let doneCount = $derived(checklist.filter((i) => i.done).length);
	let setupResumable = $derived(setup?.status === 'in_progress' && doneCount < checklist.length);

	function shortTime(iso) {
		return iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
	}
</script>

<div class="page">
	<PageHeader
		title={t('dashboard.title')}
		subtitle={loading || loadError
			? ''
			: t('dashboard.subtitle', {
					devices: t('dashboard.devicesCount', { count: devices.length }),
					areas: t('dashboard.areasCount', { count: areas.length }),
				})}
	>
		{#snippet actions()}
			<a class="btn" href="{base}/notifications?compose=1"><Icon name="bell" />{t('dashboard.sendMessage')}</a>
			<button type="button" class="btn btn-primary" onclick={() => openPairing('member')} data-testid="open-pairing">
				<Icon name="qr" />{t('pairing.open')}
			</button>
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if loadError}
		<div class="card"><ErrorState title={t('dashboard.loadError')} message={loadError} onretry={loadAll} /></div>
	{:else}
		<section class="kpis" aria-label={t('dashboard.title')}>
			<div class="card kpi" data-testid="kpi-online">
				<b>{t('dashboard.kpi.online', { online, total: devices.length })}</b>
				<span>{t('dashboard.kpi.onlineLabel')}</span>
				<small>{t('dashboard.kpi.onlineNote')}</small>
			</div>
			<div class="card kpi">
				<b>{areas.length}</b>
				<span>{t('dashboard.kpi.areas')}</span>
				<small>{t('dashboard.kpi.areasInactive', { count: inactiveAreas })}</small>
			</div>
			<div class="card kpi">
				<b>{guests.length}</b>
				<span>{guests.length ? t('dashboard.kpi.guests', { count: guests.length }) : t('dashboard.kpi.guestsNone')}</span>
				{#if guests[0]?.expires_at}
					<small>{t('dashboard.kpi.guestNext', { name: guests[0].owner_label || guests[0].name, time: formatTimeRemaining(guests[0].expires_at) })}</small>
				{/if}
			</div>
			<div class="card kpi">
				<b>{notifications.length}</b>
				<span>{t('dashboard.kpi.notifications', { count: notifications.length })}</span>
				<small>
					{lastNotification
						? t('dashboard.kpi.notificationsLast', { time: shortTime(lastNotification.created_at), title: lastNotification.title || '' })
						: t('dashboard.kpi.notificationsNone')}
				</small>
			</div>
		</section>

		{#if integration && !integration.connected}
			<Banner tone="warn" class="mt-3" testid="integration-offline">
				{t('dashboard.integrationOffline')}
				{#snippet actions()}<a class="btn btn-sm" href="{base}/settings?tab=system">{t('dashboard.openSettings')}</a>{/snippet}
			</Banner>
		{/if}
		<HaCleartextHint {haUrls} class="mt-3" />
		{#each slowSetup as c (c.id)}
			<Banner tone="warn" class="mt-3">
				{t('dashboard.setupSlow', { name: c.name })}
				{#snippet actions()}<a class="btn btn-sm" href="{base}/clients/{c.id}">{t('dashboard.viewDevice')}</a>{/snippet}
			</Banner>
		{/each}

		<SetupCard {checklist} resumable={setupResumable} class="mt-3.5" />

		<div class="two">
			<RecentImportant />

			<section class="card" aria-labelledby="dash-devices">
				<div class="card-head">
					<Icon name="iphone" />
					<h2 id="dash-devices" class="flex-1">{t('dashboard.devicesCard')}</h2>
					<a class="btn btn-ghost btn-sm" href="{base}/clients">{t('dashboard.allDevices')}<Icon name="chev" /></a>
				</div>
				{#if devices.length === 0}
					<EmptyState compact icon="qr" title={t('dashboard.noDevices')}>
						<button type="button" class="btn btn-primary" onclick={() => openPairing('member')}><Icon name="qr" />{t('pairing.open')}</button>
					</EmptyState>
				{:else}
					<ul class="list">
						{#each devices.slice(0, 6) as c (c.id)}
							<li>
								<Avatar name={c.owner_label} />
								<a class="dev-link" href="{base}/clients/{c.id}">
									<b>{c.name}</b>
									<small>
										{#if c.ha_token_status === 'pending'}{t('dashboard.settingUp')}{:else if c.is_connected}{c.owner_label || '–'}{:else}{t('dashboard.lastSeen', { time: formatRelativeDate(c.last_seen) })}{/if}
									</small>
								</a>
								{#if c.access === 'guest'}<span class="badge badge-guest">{t('pairing.roles.guest')}</span>{/if}
								<StatusBadge online={c.is_connected} />
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>

		<section class="card mt-3" aria-labelledby="dash-areas">
			<div class="card-head">
				<Icon name="grid" />
				<h2 id="dash-areas" class="flex-1">{t('dashboard.areasCard')}</h2>
				<a class="btn btn-ghost btn-sm" href="{base}/areas">{t('dashboard.allAreas')}<Icon name="chev" /></a>
			</div>
			{#if areas.length === 0}
				<EmptyState compact icon="grid" title={t('dashboard.noAreas')}>
					<a class="btn btn-primary" href="{base}/areas"><Icon name="plus" />{t('dashboard.createArea')}</a>
				</EmptyState>
			{:else}
				<div class="area-grid">
					{#each areas as a (a.id)}
						<a class="area" class:inactive={!a.is_active} href="{base}/areas/{a.id}">
							<b>{a.name}</b>
							<small>
								{t('dashboard.entities', { count: a.entity_count || 0 })}{#if !a.is_active}{' · '}{t('dashboard.inactive')}{/if}
							</small>
						</a>
					{/each}
				</div>
			{/if}
		</section>
	{/if}
</div>

<style>
	.kpis {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 14px;
	}
	.two {
		display: grid;
		grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
		gap: 14px;
		margin-top: 14px;
	}
	.dev-link {
		flex: 1;
		min-width: 0;
		color: inherit;
		text-decoration: none;
	}
	.dev-link:hover b {
		text-decoration: underline;
	}
	.dev-link b,
	.area b {
		display: block;
		font-weight: 650;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dev-link small,
	.area small {
		display: block;
		color: var(--ink-3);
		font-size: var(--fs-cap);
	}
	.area-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
		gap: 8px;
	}
	.area {
		display: block;
		padding: 10px 12px;
		border-radius: var(--r-box);
		background: var(--surface-2);
		color: inherit;
		text-decoration: none;
		min-height: 44px;
		box-sizing: border-box;
	}
	.area:hover {
		background: var(--fill-2);
	}
	.area.inactive b {
		color: var(--ink-2);
	}
	@media (max-width: 1023px) {
		.kpis {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.two {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	@media (max-width: 420px) {
		.kpis :global(.kpi b) {
			font-size: 22px;
		}
	}
</style>
