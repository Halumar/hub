<script>
	import { page } from '$app/stores';
	import { base } from '$app/paths';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onMount, onDestroy } from 'svelte';
	import { copyText, formatDate, formatRelativeDate, formatTimeRemaining, formatWeekdayTime, formatHoursShort, toLocalInput } from '$lib/utils.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import DeleteDeviceDialog from '$lib/components/DeleteDeviceDialog.svelte';
	import DeviceProfile from './components/DeviceProfile.svelte';
	import PanelCard from './components/PanelCard.svelte';
	import { deviceUserName, isSetupSlow } from '$lib/device-user.js';
	import { reasonText } from '$lib/profiles/catalog.svelte.js';
	import { previewExtend } from '$lib/profiles/preview.js';
	import RemoteKeyWarning from '$lib/components/profiles/RemoteKeyWarning.svelte';
	import { remoteState, accessLabel, activityIcon, activityLabel, appVersionLabel, deviceIcon, haAccessOf, isWatch, platformLabel } from '$lib/device-info.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import StatusBadge from '$lib/ui/StatusBadge.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';

	let client = $state(null);
	/** Effective rules (from the profile card): the Home Assistant access of the device. */
	let effective = $state(null);
	let accessReason = $derived(effective?.client_id === client?.id ? effective?.rules?.control_path?.reason : null);
	let access = $derived(haAccessOf(client, effective?.client_id === client?.id ? effective?.rules?.control_path?.value : null));
	let loading = $state(true);
	let errorMsg = $state('');
	let clientId = $state('');

	// Edit state
	let editing = $state(false);
	let editName = $state('');
	let editSaving = $state(false);

	// Home Assistant user of this device: owner label
	let ownerInput = $state('');
	let ownerSaving = $state(false);
	let pendingPoll = null;

	// Extend guest state
	let showExtend = $state(false);
	let extendMode = $state('hours');
	let extendHours = $state(24);
	let extendDatetime = $state('');
	let extendSaving = $state(false);


	// Activity feed: newest first, a few shown until expanded
	const ACTIVITY_PREVIEW = 8;
	let activity = $state([]);
	let activityMax = $state(100);
	let activityError = $state('');
	let activityExpanded = $state(false);

	// Notification state
	let clientNotifications = $state([]);
	let quickNotifTitle = $state('');
	let quickSending = $state(false);
	let clearingNotifs = $state(false);

	// Delete busy state
	let deleting = $state(false);

	// Validation feedback
	let editNameError = $state('');

	// Token rotation (the integration mints the new token)
	let rotating = $state(false);

	// Confirm dialogs
	let confirmDeleteOpen = $state(false);
	let deleteDeviceOpen = $state(false);
	let confirmClearOpen = $state(false);
	let confirmRotateOpen = $state(false);

	let unsubs = [];

	$effect(() => {
		const id = $page.params.id;
		if (id && id !== clientId) {
			clientId = id;
			loadClient(id);
		}
	});

	// While the integration sets up the device's HA user, refresh until done.
	$effect(() => {
		const waiting = client?.ha_token_status === 'pending' && client?.is_active && !client?.pending_deletion;
		if (waiting && !pendingPoll) {
			pendingPoll = setInterval(() => loadClient(clientId), 3000);
		} else if (!waiting && pendingPoll) {
			clearInterval(pendingPoll);
			pendingPoll = null;
		}
	});

	onMount(() => {
		unsubs.push(
			onBroadcast('client_connected', (data) => {
				if (data?.client_id === clientId) loadClient(clientId);
			}),
			onBroadcast('client_disconnected', (data) => {
				if (data?.client_id === clientId) loadClient(clientId);
			}),
			onBroadcast('client_status_updated', (data) => {
				if (data?.client_id === clientId || data?.id === clientId) loadClient(clientId);
			}),
			onBroadcast('client_kicked', (data) => {
				if (data?.client_id === clientId) {
					addToast(t(data.reason === 'left' ? 'devices.leftItself' : 'devices.kickedElsewhere'), 'warning');
					// In-app navigation keeps the toast visible on the list.
					goto(`${base}/clients`);
				}
			}),
			onBroadcast('guests_expired', () => {
				if (client?.access === 'guest') loadClient(clientId);
			}),
			onBroadcast('device_activity', (data) => {
				if (data?.client_id !== clientId || activity.some((e) => e.id === data.id)) return;
				activity = [data, ...activity].slice(0, activityMax);
				if (data.event === 'upgrade_required' || data.event === 'app_updated') loadClient(clientId);
			}),
			// Notification broadcasts
			onBroadcast('notification_created', (data) => {
				if (data?.client_id === clientId) loadNotifications();
			}),
			onBroadcast('notification_delivered', (data) => {
				if (data?.client_id === clientId) loadNotifications();
			}),
			onBroadcast('notification_deleted', (data) => {
				if (data?.client_id === clientId) loadNotifications();
			}),
			onBroadcast('notifications_cleared', (data) => {
				if (data?.client_id === clientId) loadNotifications();
			}),
		);
	});

	onDestroy(() => {
		unsubs.forEach(fn => fn());
		if (pendingPoll) clearInterval(pendingPoll);
	});

	async function loadClient(id) {
		try {
			client = await api.getClient(id);
			errorMsg = '';
			if (!ownerSaving) ownerInput = client?.owner_label || '';
			loadNotifications();
			loadActivity(id);
		} catch (e) {
			if (e.status === 404 || !client) {
				errorMsg = e.status === 404 ? t('devices.notFound') : t('common.couldNotLoad', { name: t('devices.title'), message: e.message });
				client = null;
			} else {
				// Keep the page on a failed background refresh.
				addToast(t('devices.refreshFailed', { message: e.message }), 'error');
			}
		} finally {
			loading = false;
		}
	}

	async function loadActivity(id) {
		try {
			const result = await api.getClientActivity(id);
			activity = Array.isArray(result?.events) ? result.events : [];
			if (Number.isInteger(result?.max_events)) activityMax = result.max_events;
			activityError = '';
		} catch (e) {
			activityError = t('devices.activityFailed', { message: e.message });
		}
	}

	async function loadNotifications() {
		try {
			const result = await api.getClientNotifications(clientId);
			clientNotifications = result.notifications || [];
		} catch (e) {
			clientNotifications = [];
		}
	}

	// 4-state derived notification status (mirrors the Notifications page):
	// delivered = green (with WS/remote from delivered_via); pending while this
	// client is connected = orange "Delivering…" (transient); pending while
	// offline = neutral blue "unconfirmed" (NOT a warning — the HA sensor path
	// has delivered it best-effort, the add-on just cannot confirm);
	// dismissed = gray.
	function notifStatus(n) {
		if (n.status === 'delivered') {
			const via = n.delivered_via === 'remote' ? 'remote' : n.delivered_via === 'ws' ? 'WS' : '';
			return { label: via ? t('devices.status.deliveredVia', { via }) : t('devices.status.delivered'), tone: 'ok' };
		}
		if (n.status === 'pending') {
			if (client?.is_connected) return { label: t('devices.status.delivering'), tone: 'warn' };
			return { label: t('devices.status.unconfirmed'), tone: 'info' };
		}
		return { label: t('devices.status.dismissed'), tone: '' };
	}

	async function handleQuickSend() {
		if (!quickNotifTitle || quickSending) return;
		quickSending = true;
		try {
			await api.createNotification({ client_id: clientId, title: quickNotifTitle });
			quickNotifTitle = '';
			addToast(t('devices.notifSent'), 'success');
		} catch (e) {
			addToast(t('devices.notifSendFailed', { message: e.message }), 'error');
		} finally {
			quickSending = false;
		}
	}

	function requestClearClientNotifs() {
		confirmClearOpen = true;
	}

	async function handleClearClientNotifs() {
		clearingNotifs = true;
		try {
			await api.clearClientNotifications(clientId);
			addToast(t('devices.notifCleared'), 'success');
		} catch (e) {
			addToast(t('devices.notifClearFailed', { message: e.message }), 'error');
		} finally {
			clearingNotifs = false;
		}
	}

	function requestRotateToken() {
		confirmRotateOpen = true;
	}

	async function handleRotateToken() {
		rotating = true;
		// A device with remote access that is away keeps its token until it is home.
		const awayWithRemote = !!client?.remote_allowed && !client?.is_connected;
		// Remote access unknown (rules not resolved): say neither "away" nor "integration".
		const remoteUnknown = client?.remote_allowed === null || client?.control_path === null;
		try {
			const result = await api.rotateClientToken(clientId);
			await loadClient(clientId);
			if (result?.status === 'pending') {
				addToast(t(remoteUnknown ? 'devices.renewQueuedUnknown' : awayWithRemote ? 'devices.renewDeferred' : 'devices.renewQueued'), 'info');
			} else if (result?.status === 'rotation_requested') {
				addToast(t('devices.renewRequested'), 'info');
			} else {
				addToast(t('devices.renewDone'), 'success');
			}
		} catch (e) {
			addToast(t('devices.renewFailed', { message: e.message }), 'error');
		} finally {
			rotating = false;
		}
	}

	async function saveOwner() {
		const value = ownerInput.trim();
		if (value === (client.owner_label || '')) return;
		if (value.length > 50) {
			addToast(t('devices.ownerTooLong'), 'error');
			return;
		}
		ownerSaving = true;
		try {
			client = await api.updateClient(clientId, { owner_label: value });
			ownerInput = client.owner_label || '';
			addToast(value ? t('devices.ownerSaved') : t('devices.ownerRemoved'), 'success');
		} catch (e) {
			addToast(t('devices.ownerFailed', { message: e.message }), 'error');
		} finally {
			ownerSaving = false;
		}
	}

	// -- Edit (name only) --
	function startEdit() {
		editName = client.name;
		editNameError = '';
		editing = true;
	}

	function cancelEdit() {
		editing = false;
	}

	function validateEditName() {
		if (!editName.trim()) {
			editNameError = t('devices.nameRequired');
			return false;
		}
		editNameError = '';
		return true;
	}

	async function saveEdit() {
		if (!validateEditName()) return;
		editSaving = true;
		try {
			await api.updateClient(clientId, { name: editName });
			await loadClient(clientId);
			editing = false;
			addToast(t('devices.renamed'), 'success');
		} catch (e) {
			addToast(t('devices.renameFailed', { message: e.message }), 'error');
		} finally {
			editSaving = false;
		}
	}

	// -- Delete (disconnect + remove) --
	function requestDelete() {
		// Member devices get the removal dialog with the offline choice.
		if (!isPending && !isGuest) deleteDeviceOpen = true;
		else confirmDeleteOpen = true;
	}

	async function deleteDevice(removal) {
		deleting = true;
		try {
			await api.deleteClient(clientId, removal);
			const key = removal === 'immediate' ? 'devices.deletedImmediate' : removal === 'on_reconnect' ? 'devices.deletedOnReconnect' : 'devices.deleted';
			addToast(t(key, { name: client.name }), 'success');
			await loadClient(clientId);
		} catch (e) {
			addToast(t('devices.removeFailed', { message: e.message }), 'error');
		} finally {
			deleting = false;
		}
	}

	async function revokeDevice() {
		deleting = true;
		try {
			await api.revokeClient(clientId);
			addToast(t('devices.revokedToast', { name: client.name }), 'success');
			await loadClient(clientId);
		} catch (e) {
			addToast(t('devices.revokeFailed', { message: e.message }), 'error');
		} finally {
			deleting = false;
		}
	}

	async function kickClient() {
		deleting = true;
		try {
			if (isPending) {
				// Already pending: just hide it from the list (background tombstone).
				await api.dismissPending(clientId);
				addToast(t('devices.listRemoved', { name: client.name }), 'success');
				window.location.href = `${base}/clients`;
			} else if (isGuest) {
				// Guests are removed immediately (hard kick + delete).
				await api.kickClient(clientId);
				addToast(t('devices.guestDeleted', { name: client.name }), 'success');
				window.location.href = `${base}/clients`;
			} else {
				// Regular clients enter the pending-deletion flow: the device is
				// signalled to reset, then its HA user is removed. Stay on the page and reflect the
				// backend "Waiting for device" state instead of vanishing.
				await api.deleteClient(clientId);
				addToast(t('devices.deleted', { name: client.name }), 'success');
				confirmDeleteOpen = false;
				await loadClient(clientId);
				deleting = false;
			}
		} catch (e) {
			addToast(t('devices.removeFailed', { message: e.message }), 'error');
			deleting = false;
		}
	}

	// -- Extend Guest --
	function openExtend() {
		extendMode = 'hours';
		extendHours = 24;
		const expiryBase = client.expires_at ? new Date(client.expires_at) : new Date();
		const d = new Date(expiryBase.getTime() + 24 * 3600000);
		extendDatetime = toLocalInput(d);
		showExtend = true;
	}

	// The chosen new end, asked of the hub first (dry run): the warning comes from its answer, and
	// Extend waits for the answer to the newest choice. No answer: the warning shows.
	let extendBody = $derived(
		extendMode === 'hours'
			? Number(extendHours) > 0
				? { hours: Number(extendHours) }
				: null
			: extendDatetime && !Number.isNaN(Date.parse(extendDatetime))
				? { expires_at: new Date(extendDatetime).toISOString() }
				: null,
	);
	let extendHub = $state(null);
	let extendPending = $state(false);
	let extendSeq = 0;
	$effect(() => {
		const body = extendBody;
		if (!showExtend || !body) {
			extendSeq += 1;
			extendHub = null;
			extendPending = false;
			return;
		}
		const s = ++extendSeq;
		extendPending = true;
		const timer = setTimeout(async () => {
			const pv = await previewExtend(clientId, body);
			if (s !== extendSeq) return;
			extendHub = pv;
			extendPending = false;
		}, 250);
		return () => clearTimeout(timer);
	});

	function pickExtendHours(h) {
		extendMode = 'hours';
		extendHours = h;
	}

	async function submitExtend(e) {
		e?.preventDefault();
		if (extendPending || !extendBody) return;
		extendSaving = true;
		try {
			let data = {};
			if (extendMode === 'hours') {
				data.hours = extendHours;
			} else {
				data.expires_at = new Date(extendDatetime).toISOString();
			}
			const result = await api.extendGuest(clientId, data);
			addToast(t('devices.extended', { date: formatWeekdayTime(result.expires_at) }), 'success');
			showExtend = false;
			await loadClient(clientId);
		} catch (e) {
			addToast(t('devices.extendFailed', { message: e.message }), 'error');
		} finally {
			extendSaving = false;
		}
	}

	async function copyId() {
		const ok = await copyText(client.id);
		addToast(ok ? t('devices.idCopied') : t('common.copyFailed'), ok ? 'success' : 'error');
	}

	let isGuest = $derived(client?.access === 'guest');
	let isExpired = $derived(isGuest && !!client?.expires_at && Date.parse(client.expires_at) <= Date.now());
	let role = $derived(!client ? 'member' : isGuest ? 'guest' : client.owner_label ? 'member' : 'shared');
	// Location: what the app shares, and whether the profile requires it.
	let locationFact = $derived.by(() => {
		if (!client?.location_rule || client.location_rule === 'off') return null;
		const shared = client.location_shared;
		const missing = shared !== 'foreground' && shared !== 'background';
		const label =
			shared === 'background' ? t('profiles.device.locationBackground')
			: shared === 'foreground' ? t('profiles.device.locationForeground')
			: shared === 'none' ? t('profiles.device.locationNone')
			: t('profiles.device.locationUnknown');
		const required = missing && client.location_required;
		return {
			label: required ? `${label} · ${t('profiles.device.locationRequired')}` : label,
			tone: required ? 'warn' : missing ? '' : 'ok',
		};
	});
	let isPending = $derived(!!client?.pending_deletion);
	let shownActivity = $derived(activityExpanded ? activity : activity.slice(0, ACTIVITY_PREVIEW));
	// Minimum app version per platform, from /api/health.
	let minAppByPlatform = $state({});
	let minAppVersion = $derived((client?.platform && minAppByPlatform[client.platform]) || '');

	onMount(async () => {
		try {
			const health = await api.health();
			if (health?.min_app_version) minAppByPlatform = health.min_app_version;
		} catch {
			/* hint without the version */
		}
	});
</script>

<div class="page">
	{#if loading}
		<LoadingState />
	{:else if !client}
		<div class="card">
			<EmptyState icon="iphone" title={errorMsg || t('devices.notFound')}>
				<a class="btn" href="{base}/clients"><Icon name="back" />{t('devices.back')}</a>
			</EmptyState>
		</div>
	{:else}
		<PageHeader title={client.name} back="{base}/clients" backLabel={t('devices.back')}>
			{#snippet actions()}
				<StatusBadge online={client.is_connected} label={client.is_connected ? t('devices.connected') : t('devices.disconnected')} />
				{#if client.is_active || isGuest}
					<button type="button" class="btn" onclick={startEdit}><Icon name="edit" />{t('devices.rename')}</button>
				{/if}
				<Menu iconOnly ariaLabel={t('devices.actionsFor', { name: client.name })} items={[{ label: t('devices.copyId'), icon: 'copy', onclick: copyId }]} />
			{/snippet}
		</PageHeader>

		{#if isPending}
			<Banner tone="error" title={t('devices.pendingTitle')} class="mb-4">{t('devices.waitingInfo')}</Banner>
		{:else if client.update_required && client.is_active}
			<Banner tone="warn" title={t('devices.updateBannerTitle')} class="mb-4" testid="update-banner">
				{client.app_version ? t('devices.updateBannerText', { min: minAppVersion || '–' }) : t('devices.updateBannerUnknown')}
			</Banner>
		{/if}

		{#if !isPending && (client.is_active || isGuest)}
			<div class="stack mb-4">
				<DeviceProfile {client} onchanged={(c) => (client = c)} onextend={!isExpired ? openExtend : null} oneffective={(e) => (effective = e)} />
			</div>
		{/if}

		<div class="detail">
			<div class="stack">
				<!-- Home Assistant user -->
				<section class="card" data-testid="ha-user-card" aria-labelledby="ha-user-h">
					<div class="card-head">
						<Icon name="user" />
						<h2 id="ha-user-h" class="flex-1">{t('devices.haUser')}</h2>
						{#if isPending}
							<span class="badge">{t('devices.haUserRemoving')}</span>
						{:else if client.ha_token_status === 'active'}
							<span class="badge badge-ok"><span class="dot"></span>{t('devices.haUserActive')}</span>
						{:else}
							<span class="badge badge-warn" data-testid="ha-user-pending">{t('devices.settingUp')}</span>
						{/if}
					</div>
					<dl class="dl box">
						<dt>{t('devices.haUser')}</dt>
						<dd data-testid="ha-user-name">{deviceUserName(client)}</dd>
						{#if client.ha_user_id}
							<dt>{t('devices.haUserId')}</dt>
							<dd class="t-mono">{client.ha_user_id}</dd>
						{/if}
						<dt>{t('devices.haGroup')}</dt>
						<dd data-testid="ha-group">{access.level === 'unknown' ? t('devices.levelUnknown') : access.level && access.level !== 'direct' ? t('devices.haGroupReadOnly') : t('devices.haGroupValue')}</dd>
						{#if access.level}
							<dt>{t('devices.haAccess')}</dt>
							<dd data-testid="ha-access" data-level={access.level}>
								{accessLabel(access.level)}
								{#if accessReason === 'platform_needs_key'}<small class="block t-help" data-testid="ha-access-reason">{reasonText('platform_needs_key', { platform: platformLabel(client.platform) || client.platform })}</small>{/if}
							</dd>
							<dt>{t('devices.haKey')}</dt>
							<dd data-testid="ha-key" data-key={access.key}>{t(`devices.key.${access.key}`)}</dd>
						{/if}
					</dl>
					{#if access.key && access.key !== 'pending'}<p class="t-help mt-2" data-testid="ha-key-hint">{t(`devices.keyHint.${access.key}`)}</p>{/if}
					{#if client.ha_token_status === 'pending' && client.is_active && !isPending}
						<p class="t-help mt-2">
							{t('devices.haSetupInfo')}
							{#if client.ha_token_pending_since}{t('devices.haSetupSince', { time: formatDate(client.ha_token_pending_since) })}{/if}
						</p>
						{#if isSetupSlow(client)}
							<Banner tone="warn" class="mt-2" testid="ha-user-slow">{t('devices.haSetupSlow')}</Banner>
						{/if}
					{/if}

					{#if client.is_active || isGuest}
						<div class="stack mt-4">
							<Field label={t('devices.owner')} hint={t('devices.ownerHint')} id="owner-label">
								{#snippet children({ id, describedby })}
									<div class="row flex-nowrap">
										<input {id} class="input" maxlength="50" aria-describedby={describedby} bind:value={ownerInput} onkeydown={(e) => e.key === 'Enter' && saveOwner()} placeholder={t('devices.ownerPlaceholder')} />
										<button type="button" class="btn" onclick={saveOwner} disabled={ownerSaving || ownerInput.trim() === (client.owner_label || '')}>{ownerSaving ? t('common.saving') : t('common.save')}</button>
									</div>
								{/snippet}
							</Field>

							{#if client.ha_token_status === 'active' && client.is_active}
								<div class="field">
									<div><button type="button" class="btn" onclick={requestRotateToken} disabled={rotating}><Icon name="refresh" />{rotating ? t('devices.renewing') : t('devices.renewToken')}</button></div>
									<p class="t-help">{t('devices.renewHint')}</p>
								</div>
							{/if}
						</div>
					{/if}
				</section>

				{#if !isPending && client.is_active}
					<PanelCard {client} />
				{/if}

				<section class="card" aria-labelledby="notif-h">
					<div class="card-head">
						<Icon name="bell" />
						<h2 id="notif-h" class="flex-1">{t('devices.notifTitle')}</h2>
						<a class="btn btn-ghost btn-sm" href="{base}/notifications">{t('devices.notifViewAll')}</a>
						{#if clientNotifications.length}
							<button type="button" class="btn btn-sm btn-danger" onclick={requestClearClientNotifs} disabled={clearingNotifs}>{clearingNotifs ? t('devices.notifClearing') : t('devices.notifClear')}</button>
						{/if}
					</div>
					{#if clientNotifications.length === 0}
						<p class="t-help">{t('devices.notifNone')}</p>
					{:else}
						<ul class="list">
							{#each clientNotifications as n (n.id)}
								{@const st = notifStatus(n)}
								<li>
									<span class="min-w-0 flex-1">
										<b class="font-semibold">{n.title || '–'}</b>
										{#if n.message}<small class="block t-help truncate">{n.message}</small>{/if}
									</span>
									<span class="badge {st.tone ? `badge-${st.tone}` : ''}"><span class="dot"></span>{st.label}</span>
								</li>
							{/each}
						</ul>
					{/if}
					{#if isWatch(client)}
						<p class="t-help mt-3 inline-flex items-center gap-1" data-testid="device-notif-watch">
							<Icon name="info" size="14" />{client.set_up_by ? t('devices.notifWatch', { phone: client.set_up_by.name }) : t('devices.notifWatchNoPhone')}
						</p>
					{:else}
					<form class="row flex-nowrap mt-3" onsubmit={(e) => { e.preventDefault(); handleQuickSend(); }}>
						<label class="sr-only" for="quick-notif">{t('devices.notifQuick')}</label>
						<input id="quick-notif" class="input" bind:value={quickNotifTitle} placeholder={t('devices.notifQuickPlaceholder')} />
						<button type="submit" class="btn btn-primary" disabled={quickSending || !quickNotifTitle}><Icon name="send" />{quickSending ? t('devices.notifSending') : t('devices.notifSend')}</button>
					</form>
					{/if}
				</section>
			</div>

			<div class="stack">
				<section class="card" aria-labelledby="facts-h">
					<div class="card-head"><Icon name="info" /><h2 id="facts-h">{t('devices.facts')}</h2></div>
					<dl class="dl">
						<dt>{t('devices.col.role')}</dt>
						<dd>{t(`devices.roles.${role}`)}</dd>
						<dt>{t('profiles.device.remote')}</dt>
						<dd data-testid="fact-remote" data-state={remoteState(client).id}>
							{remoteState(client).label}
							<small class="block t-help">{t('devices.remoteHintRule')}</small>
						</dd>
						{#if client.location_rule}
							<dt>{t('profiles.device.location')}</dt>
							<dd data-testid="fact-location">
								{#if locationFact}
									<span class="badge {locationFact.tone ? `badge-${locationFact.tone}` : ''}" data-shared={client.location_shared ?? 'unknown'}><span class="dot"></span>{locationFact.label}</span>
									{#if client.location_at}<small class="block t-help">{t('profiles.device.locationAt', { time: formatRelativeDate(client.location_at) })}</small>{/if}
								{:else}
									{t('profiles.device.locationRuleOff')}
								{/if}
							</dd>
						{/if}
						<dt>{t('devices.paired')}</dt>
						<dd>{formatDate(client.created_at)}</dd>
						<dt>{t('devices.lastSeen')}</dt>
						<dd>{client.is_connected ? t('devices.connected') : formatDate(client.last_seen)}</dd>
						{#if isGuest && client.expires_at}
							<dt>{t('devices.expires')}</dt>
							<dd>
								{formatWeekdayTime(client.expires_at)}
								{#if !isExpired && !isPending}<button type="button" class="btn btn-sm mt-1" onclick={openExtend} data-testid="fact-extend"><Icon name="plus" />{t('devices.extend')}</button>{/if}
							</dd>
						{/if}
						<dt>{t('devices.deviceType')}</dt>
						<dd data-testid="device-type">
							<span class="inline-flex items-center gap-1"><Icon name={deviceIcon(client)} size="14" />{platformLabel(client.platform) || t('devices.appUnknown')}</span>
						</dd>
						{#if client.device_model}
							<dt>{t('devices.deviceModel')}</dt>
							<dd class="t-mono">{client.device_model}</dd>
						{/if}
						<dt>{t('devices.appVersion')}</dt>
						<dd data-testid="app-version">
							{appVersionLabel(client) || t('devices.appUnknown')}
							{#if client.update_required && client.is_active}<span class="badge badge-warn ml-1">{t('devices.updateRequired')}</span>{/if}
						</dd>
						{#if client.protocol_version}
							<dt>{t('devices.protocolVersion')}</dt>
							<dd>v{client.protocol_version}</dd>
						{/if}
						<dt>{t('devices.clientId')}</dt>
						<dd class="t-mono select-all">{client.id}</dd>
					</dl>
				</section>

				<section class="card" aria-labelledby="activity-h" data-testid="activity-card">
					<div class="card-head"><Icon name="history" /><h2 id="activity-h">{t('devices.activityTitle')}</h2></div>
					<p class="t-help mb-3">{client.child_protection ? t('kids.device.activityHint', { count: activityMax }) : t('devices.activityHint', { count: activityMax })}</p>
					{#if activityError}
						<p class="t-help" role="alert">{activityError}</p>
					{:else if activity.length === 0}
						<p class="t-help">{t('devices.activityNone')}</p>
					{:else}
						<ol class="feed" data-testid="activity-feed">
							{#each shownActivity as entry (entry.id)}
								<li class:warn={entry.event === 'upgrade_required'}>
									<Icon name={activityIcon(entry.event)} />
									<span class="min-w-0 flex-1">{activityLabel(entry)}</span>
									<time class="t-help" datetime={entry.ts} title={formatDate(entry.ts)}>{formatRelativeDate(entry.ts)}</time>
								</li>
							{/each}
						</ol>
						{#if activity.length > ACTIVITY_PREVIEW}
							<button type="button" class="btn btn-ghost btn-sm mt-2" onclick={() => (activityExpanded = !activityExpanded)}>
								{activityExpanded ? t('devices.activityLess') : t('devices.activityMore', { count: activity.length })}
							</button>
						{/if}
					{/if}
				</section>

				<section class="danger-zone" aria-labelledby="danger-h">
					<h2 id="danger-h">{t('devices.dangerZone')}</h2>
					<p class="t-help mb-3">{isPending ? t('devices.dangerTextPending') : isGuest ? t('devices.dangerTextGuest') : t('devices.dangerText')}</p>
					<button type="button" class="btn btn-danger" onclick={requestDelete} disabled={deleting} data-testid="device-remove">
						<Icon name="trash" />{isPending ? t('devices.removeFromList') : isGuest ? t('devices.removeGuest') : t('devices.remove')}
					</button>
				</section>
			</div>
		</div>
	{/if}
</div>

<Modal bind:open={editing} title={t('devices.renameTitle')} size="sm" dismissible={!editSaving}>
	<form id="rename-device" onsubmit={(e) => { e.preventDefault(); saveEdit(); }}>
		<Field label={t('devices.deviceName')} required error={editNameError} id="device-name">
			{#snippet children({ id, describedby, invalid })}
				<input {id} class="input" required aria-describedby={describedby} aria-invalid={invalid} bind:value={editName} oninput={() => editNameError && validateEditName()} />
			{/snippet}
		</Field>
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={editSaving}>{t('common.cancel')}</button>
		<button type="submit" form="rename-device" class="btn btn-primary" disabled={editSaving}>{editSaving ? t('common.saving') : t('common.save')}</button>
	{/snippet}
</Modal>

<Modal bind:open={showExtend} title={t('devices.extendTitle')} size="md" dismissible={!extendSaving} testid="extend-dialog">
	<div class="stack">
		<fieldset class="field">
			<legend class="field-label mb-1.5">{t('devices.extendQuick')}</legend>
			<div class="row">
				{#each [1, 4, 8, 24, 48, 72, 168] as h (h)}
					<button type="button" class="chip" aria-pressed={extendMode === 'hours' && Number(extendHours) === h ? 'true' : 'false'} onclick={() => pickExtendHours(h)} disabled={extendSaving} data-testid="extend-quick-{h}">+{formatHoursShort(h)}</button>
				{/each}
			</div>
		</fieldset>
		<form id="extend-form" class="stack border-t border-line pt-4" onsubmit={submitExtend}>
			<span class="t-label">{t('devices.extendManual')}</span>
			<Segmented label={t('devices.extendMode')} options={[{ value: 'hours', label: t('devices.extendHours') }, { value: 'datetime', label: t('devices.extendDate') }]} bind:value={extendMode} />
			{#if extendMode === 'hours'}
				<Field label={t('devices.hoursFromNow')} id="extend-hours">
					{#snippet children({ id })}<input {id} class="input w-32" type="number" min="1" max="2160" bind:value={extendHours} />{/snippet}
				</Field>
			{:else}
				<Field label={t('devices.newExpiry')} id="extend-date">
					{#snippet children({ id })}<input {id} class="input" type="datetime-local" bind:value={extendDatetime} />{/snippet}
				</Field>
			{/if}
		</form>
		{#if extendHub && (extendHub.now || extendHub.at || extendHub.unknown)}
			{#if extendHub.unknown}<p class="t-help" data-testid="extend-unknown">{t('guests.extendUnknown')}</p>{/if}
			<RemoteKeyWarning from={!extendHub.now && extendHub.at ? extendHub.at : null} testid="extend-remote-key-warning" />
		{/if}
	</div>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={extendSaving}>{t('common.cancel')}</button>
		<button type="submit" form="extend-form" class="btn btn-primary" disabled={extendSaving || extendPending || !extendBody} data-testid="extend-save">{extendSaving ? t('common.saving') : t('devices.extend')}</button>
	{/snippet}
</Modal>

<DeleteDeviceDialog bind:open={deleteDeviceOpen} {client} busy={deleting} onremove={deleteDevice} onrevoke={revokeDevice} />
<ConfirmDialog
	bind:open={confirmDeleteOpen}
	title={isPending ? t('devices.confirmListTitle') : isGuest ? t('devices.confirmGuestTitle') : t('devices.confirmDeleteTitle')}
	message={client ? (isPending ? t('devices.confirmListMessage') : isGuest ? t('devices.confirmGuestMessage', { name: client.name }) : t('devices.confirmDeleteMessage', { name: client.name })) : ''}
	confirmLabel={isPending ? t('devices.removeFromList') : t('common.remove')}
	danger
	onconfirm={kickClient}
/>
<ConfirmDialog bind:open={confirmClearOpen} title={t('devices.notifClearTitle')} message={t('devices.notifClearMessage')} confirmLabel={t('devices.notifClear')} danger onconfirm={handleClearClientNotifs} />
<ConfirmDialog bind:open={confirmRotateOpen} title={t('devices.renewTitle')} message={client ? t('devices.renewMessage', { name: client.name }) : ''} confirmLabel={t('devices.renewConfirm')} onconfirm={handleRotateToken} />

<style>
	.detail {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
		gap: 14px;
		align-items: start;
	}
	.feed {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 2px;
	}
	.feed li {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px 0;
		border-bottom: 1px solid var(--line);
		font-size: var(--fs-sm);
	}
	.feed li:last-child {
		border-bottom: 0;
	}
	.feed li :global(.ic) {
		width: 16px;
		height: 16px;
		color: var(--ink-3);
		flex: none;
	}
	.feed li.warn :global(.ic) {
		color: var(--warn-ink);
	}
	.feed time {
		white-space: nowrap;
	}
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
	}
	@media (max-width: 1023px) {
		.detail {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
