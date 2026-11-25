<script>
	import { api } from '$lib/api.js';
	import { page } from '$app/stores';
	import { t } from '$lib/i18n/index.svelte.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import IconPicker from '$lib/components/IconPicker.svelte';
	import AddEntityModal from '../areas/[id]/components/AddEntityModal.svelte';
	import { formatDateTime, formatWeekdayTime, clientName, toLocalInput, debounce } from '$lib/utils.js';
	import { isWatch } from '$lib/device-info.js';

	let notifications = $state([]);
	let clients = $state([]);
	let loading = $state(true);
	let loadError = $state('');
	let showHistory = $state(false);
	// Companion-integration connectivity (null = unknown/not yet loaded).
	// Only used to refine the "unconfirmed" status hint — remote confirmation
	// of a sensor-delivered notification requires the integration.
	let integrationConnected = $state(null);

	// ── Compose form state ────────────────────────────────────────────────
	// notification_type uses the canonical {'default','sticky'}.
	// popup_mode uses {'none','popup','both','direct'} — see DISPLAYS below.
	// 'direct' immediately opens the linked area/entity/url when shown (one-time, no card);
	// it REQUIRES an action (area/entity/url) — backend rejects 'direct' + action_type 'none'.
	let showSendForm = $state(false);
	let sendClientId = $state('');
	let sendTitle = $state('');          // REQUIRED
	let sendMessage = $state('');        // optional
	let sendType = $state('default');    // default | sticky
	let sendIcon = $state('');
	let sendPopupMode = $state('none');  // none | popup | both | direct
	let sendPriority = $state(50);       // int 0..100, 0 = top of the stack
	let sendColor = $state('none');      // none|red|blue|green|yellow|orange
	let sendActionType = $state('none'); // none|area|entity|url
	let sendActionTarget = $state('');   // area_id | entity_id | URL
	let sendActionEntityName = $state(''); // friendly name for the chosen entity (display only)
	let sendShowTime = $state(true);     // per-notification: show the send-time in the app (default on)

	const TYPES = ['default', 'sticky'];
	const DISPLAYS = ['none', 'popup', 'both', 'direct'];
	const COLORS = ['none', 'red', 'blue', 'green', 'yellow', 'orange'];

	// Area targets come from the add-on's OWN areas (api.getAreas) — the same ids the
	// app navigates to via navigate_to_area / remoteNavigationAreaId. Entity targets
	// are picked via the shared AddEntityModal (full HA entity list).
	let addonAreas = $state([]);
	let haEntities = $state([]);

	// Entity-picker modal (reuses areas' AddEntityModal).
	let showEntityPicker = $state(false);
	let entityPickerSectionId = $state(null); // bindable sink; modal hides the selector when sections=[]
	const noExistingEntities = new Set(); // never filter out entities in the notification picker

	// Reset the target whenever the action type changes (ids/URLs aren't interchangeable).
	function onActionTypeChange(type) {
		sendActionType = type;
		sendActionTarget = '';
		sendActionEntityName = '';
	}

	function openEntityPicker() {
		showEntityPicker = true;
	}

	// Link/Action section element — used to scroll/hint when "Direct Open" needs a link.
	let linkSectionEl = $state(null);

	// Display segmented handler: selecting "Direct Open" without a link nudges the user
	// to the Link section (Direct Open shows nothing, it just opens the target).
	function onDisplayPick(v) {
		sendPopupMode = v;
		if (v === 'direct' && sendActionType === 'none') {
			linkSectionEl?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		}
	}

	// AddEntityModal callback: (entity_id, friendly_name).
	function onEntityPicked(entityId, friendlyName) {
		sendActionType = 'entity';
		sendActionTarget = entityId;
		sendActionEntityName = friendlyName || '';
		showEntityPicker = false;
	}

	// Priority is an int 0..100 (0 = highest/top, 100 = lowest/bottom).
	function clampPriority(v) {
		const n = Math.round(Number(v));
		if (!Number.isFinite(n)) return 50;
		return Math.min(100, Math.max(0, n));
	}
	function stepPriority(delta) {
		sendPriority = clampPriority(sendPriority + delta);
	}

	let scheduleMode = $state('now'); // 'now' | 'schedule'
	let sendScheduleAt = $state(''); // datetime-local string (device local time)
	let schedulePreset = $state(''); // '1h' | 'tonight' | 'tomorrow' | 'custom' | ''
	let sending = $state(false);

	const SCHEDULE_PRESETS = ['1h', 'tonight', 'tomorrow', 'custom'];

	function atTime(base, h, m) { const d = new Date(base); d.setHours(h, m, 0, 0); return d; }
	// The next calendar day in local time (not now + 24 h, which skips or repeats
	// a day around a daylight saving change).
	function nextDay(base) { const d = new Date(base); d.setDate(d.getDate() + 1); return d; }

	function applyPreset(key) {
		schedulePreset = key;
		const now = new Date();
		if (key === '1h') {
			sendScheduleAt = toLocalInput(new Date(now.getTime() + 3_600_000));
		} else if (key === 'tonight') {
			let d = atTime(now, 18, 0);
			if (d <= now) d = atTime(nextDay(now), 18, 0); // rolls to tomorrow if past
			sendScheduleAt = toLocalInput(d);
		} else if (key === 'tomorrow') {
			sendScheduleAt = toLocalInput(atTime(nextDay(now), 8, 0));
		} else if (key === 'custom') {
			if (!sendScheduleAt) sendScheduleAt = toLocalInput(new Date(now.getTime() + 3_600_000));
		}
	}

	// When the user hand-edits the picker, the active chip is no longer authoritative.
	function onPickerInput() { schedulePreset = 'custom'; }

	let schedulePreview = $derived.by(() => {
		if (scheduleMode !== 'schedule' || !sendScheduleAt) return '';
		const d = new Date(sendScheduleAt);
		if (isNaN(d.getTime())) return '';
		return formatWeekdayTime(d);
	});

	let scheduleInPast = $derived.by(() =>
		!!sendScheduleAt && new Date(sendScheduleAt).getTime() < Date.now()
	);

	function clearSchedule() { scheduleMode = 'now'; sendScheduleAt = ''; schedulePreset = ''; }

	let sendErrors = $state({ client: '', title: '' });

	// Title is the ONLY required field — message is optional. The Send button is
	// enabled only when a title is present (and the schedule, if any, is valid).
	let titleMissing = $derived(!sendTitle.trim());
	let scheduleInvalid = $derived(scheduleMode === 'schedule' && (!sendScheduleAt || scheduleInPast));
	// "Direct Open" displays nothing — it just opens the link — so it REQUIRES an action.
	let directNeedsAction = $derived(sendPopupMode === 'direct' && sendActionType === 'none');
	let canSend = $derived(!sending && !titleMissing && !!sendClientId && !scheduleInvalid && !directNeedsAction);

	// ── Area visibility warning ───────────────────────────────────────────
	// Tag model: an area is VISIBLE to a client when the area has no tags, OR the
	// client has no tags, OR the area's tags intersect the client's tags. Both
	// clients (getPairedClients) and areas (getAreas) carry a `tags: string[]`.
	// We only warn for area links — entity links can't resolve their add-on area
	// cheaply (the areas list has no entities), so we skip the entity warning.
	function areaVisibleToClient(areaTags, clientTags) {
		const a = Array.isArray(areaTags) ? areaTags : [];
		const c = Array.isArray(clientTags) ? clientTags : [];
		if (a.length === 0 || c.length === 0) return true; // untagged area or untagged client sees everything
		return a.some((t) => c.includes(t)); // intersection
	}

	// The add-on area selected as the area link target (with its tags), if any.
	let linkedArea = $derived(
		sendActionType === 'area' && sendActionTarget
			? addonAreas.find((a) => a.id === sendActionTarget) || null
			: null
	);

	// The selected recipient client (a specific one; 'all'/'' resolve to null).
	let selectedClient = $derived(
		sendClientId && sendClientId !== 'all'
			? clients.find((c) => c.id === sendClientId) || null
			: null
	);

	// Specific-client warning: chosen area link is NOT visible to that client.
	// Best-effort only for areas; entity/url never trigger it.
	let areaNotVisibleToClient = $derived(
		!!linkedArea &&
			!!selectedClient &&
			!areaVisibleToClient(linkedArea.tags, selectedClient.tags)
	);

	// Broadcast soft-note: a tagged area may be hidden on some devices.
	let areaMayBeHiddenForSome = $derived(
		sendClientId === 'all' &&
			!!linkedArea &&
			Array.isArray(linkedArea.tags) &&
			linkedArea.tags.length > 0
	);

	// Per-action busy flags
	let deletingId = $state(null);
	let clearingClientId = $state(null);

	// Confirm dialog state
	let confirmOpen = $state(false);
	let confirmTitle = $state('');
	let confirmMessage = $state('');
	let confirmLabel = $state('');
	let confirmAction = $state(() => {});

	let unsubs = [];

	onMount(async () => {
		if ($page.url.searchParams.get('compose')) showSendForm = true;
		await loadData();
		// Targets for the action picker. Best-effort: fall back to a text input if unavailable.
		// Areas = the add-on's own areas (api.getAreas), NOT Home Assistant areas.
		try {
			const [areas, entities] = await Promise.all([
				api.getAreas().catch(() => []),
				api.getHAEntities().catch(() => []),
			]);
			addonAreas = Array.isArray(areas) ? areas : [];
			haEntities = Array.isArray(entities) ? entities : [];
		} catch {
			addonAreas = [];
			haEntities = [];
		}
		unsubs.push(
			onBroadcast('notification_created', refreshData),
			onBroadcast('notification_delivered', refreshData),
			onBroadcast('notification_deleted', refreshData),
			onBroadcast('notifications_cleared', refreshData),
			// Connectivity flips move pending notifications between the
			// "Delivering…" and "unconfirmed" derived states — reload live.
			onBroadcast('client_connected', refreshData),
			onBroadcast('client_disconnected', refreshData),
			onBroadcast('resync', refreshData),
		);
	});

	// Bursts of live events collapse into one quiet reload.
	const refreshData = debounce(() => loadData(), 250);

	onDestroy(() => {
		refreshData.cancel();
		unsubs.forEach(fn => fn());
	});

	async function loadData() {
		try {
			const [notifRes, clientList, integration] = await Promise.all([
				api.getNotifications(showHistory ? 'all' : 'active'),
				api.getPairedClients(),
				// Best-effort: an error keeps the state "unknown" (no hint).
				api.integrationStatus().catch(() => null),
			]);
			notifications = notifRes?.notifications || (Array.isArray(notifRes) ? notifRes : []);
			// getPairedClients returns an array directly, not {clients: [...]}
			clients = Array.isArray(clientList) ? clientList : [];
			integrationConnected = integration ? integration.connected === true : null;
			loadError = '';
		} catch (err) {
			loadError = t('notifications.loadFailed', { message: err.message });
		} finally {
			loading = false;
		}
	}

	function validateSend() {
		const errs = { client: '', title: '' };
		if (!sendClientId) errs.client = t('notifications.selectClientError');
		if (!sendTitle.trim()) errs.title = t('notifications.titleRequired');
		sendErrors = errs;
		return !errs.client && !errs.title;
	}

	function resetForm() {
		sendTitle = '';
		sendMessage = '';
		sendType = 'default';
		sendIcon = '';
		sendPopupMode = 'none';
		sendPriority = 50;
		sendColor = 'none';
		sendActionType = 'none';
		sendActionTarget = '';
		sendActionEntityName = '';
		sendShowTime = true;
		scheduleMode = 'now'; sendScheduleAt = ''; schedulePreset = '';
		sendErrors = { client: '', title: '' };
	}

	async function handleSend() {
		if (!validateSend()) return;
		// 'direct' opens the link immediately on display, so it must have a target.
		if (directNeedsAction) {
			addToast(t('notifications.directNeedsLink'), 'error');
			return;
		}
		sending = true;
		try {
			const payload = {
				client_id: sendClientId,                    // a client id, or 'all' to broadcast to every active client
				title: sendTitle.trim(),
				message: sendMessage,                       // optional
				notification_type: sendType,                // 'default' | 'sticky'
				popup_mode: sendPopupMode,                  // 'none' | 'popup' | 'both' | 'direct'
				popup: sendPopupMode === 'popup' || sendPopupMode === 'both', // still in the contract (stored column)
				priority: clampPriority(sendPriority),      // int 0..100, 0 = highest (top)
				color: sendColor,                           // none|red|blue|green|yellow|orange
				action_type: sendActionType,                // none|area|entity|url
				action_target: sendActionType === 'none' ? '' : sendActionTarget,
				show_time: sendShowTime,                    // per-notification: show send-time in the app (default true)
			};
			if (sendIcon) payload.icon = sendIcon;
			// Convert the local datetime-local value to an ISO-8601 UTC string.
			if (scheduleMode === 'schedule' && sendScheduleAt) {
				payload.scheduled_at = new Date(sendScheduleAt).toISOString();
			}
			const result = await api.createNotification(payload);
			const scheduled = !!payload.scheduled_at;
			addToast(
				result.ws_sent
					? t(scheduled ? 'notifications.sentWsScheduled' : 'notifications.sentWs')
					: t(scheduled ? 'notifications.sentQueuedScheduled' : 'notifications.sentQueued'),
				result.ws_sent ? 'success' : 'warning'
			);
			resetForm();
			showSendForm = false;
		} catch (err) {
			addToast(t('notifications.sendFailed', { message: err.message }), 'error');
		} finally {
			sending = false;
		}
	}

	function askDelete(n) {
		confirmTitle = t('notifications.deleteTitle');
		confirmMessage = t('notifications.deleteMessage');
		confirmLabel = t('common.delete');
		confirmAction = () => handleDelete(n.id);
		confirmOpen = true;
	}

	async function handleDelete(id) {
		deletingId = id;
		try {
			await api.deleteNotification(id);
			addToast(t('notifications.deleted'), 'success');
		} catch (err) {
			addToast(t('notifications.deleteFailed', { message: err.message }), 'error');
		} finally {
			deletingId = null;
		}
	}

	function askClearClient(clientId, name) {
		confirmTitle = t('notifications.clearTitle');
		confirmMessage = t('notifications.clearMessage', { name });
		confirmLabel = t('notifications.clearAll');
		confirmAction = () => handleClearClient(clientId, name);
		confirmOpen = true;
	}

	async function handleClearClient(clientId, name) {
		clearingClientId = clientId;
		try {
			const result = await api.clearClientNotifications(clientId);
			addToast(t('notifications.cleared', { count: result?.cleared ?? 0, name }), 'success');
		} catch (err) {
			addToast(t('notifications.clearFailed', { message: err.message }), 'error');
		} finally {
			clearingClientId = null;
		}
	}

	// ── 4-state derived notification status ──────────────────────────────
	// The DB status stays three-valued (pending/delivered/dismissed, plus
	// delivered_via); client connectivity splits "pending" into an honest pair:
	//   delivered                  -> green "Delivered (WS|remote)"
	//   pending + client connected -> orange "Delivering…" (transient — if it
	//                                 sticks >1 min the app predates acks)
	//   pending + client offline   -> neutral blue "Sent via HA sensor —
	//                                 unconfirmed" (NOT a warning color: the HA
	//                                 sensor path has delivered it best-effort,
	//                                 the add-on just cannot confirm)
	//   dismissed                  -> gray
	function notifStatus(n) {
		if (n.status === 'delivered') {
			const via = n.delivered_via === 'remote' ? 'remote' : n.delivered_via === 'ws' ? 'WS' : '';
			return { key: 'delivered', label: via ? t('notifications.statuses.deliveredVia', { via }) : t('notifications.statuses.delivered'), tone: 'ok', hint: '' };
		}
		if (n.status === 'pending') {
			const client = clients.find((c) => c.id === n.client_id);
			if (client?.is_connected) return { key: 'delivering', label: t('notifications.statuses.delivering'), tone: 'warn', hint: '' };
			return {
				key: 'unconfirmed',
				label: t('notifications.statuses.unconfirmed'),
				tone: 'info',
				hint: integrationConnected === false ? t('notifications.unconfirmedHint') : '',
			};
		}
		return { key: 'dismissed', label: t('notifications.statuses.dismissed'), tone: '', hint: '' };
	}

	function canonicalType(type) {
		return type === 'sticky' ? 'sticky' : 'default';
	}

	function displayMode(n) {
		return DISPLAYS.includes(n.popup_mode) ? n.popup_mode : 'none';
	}

	function actionLabel(n) {
		const kind = ['area', 'entity', 'url'].includes(n.action_type) ? n.action_type : 'none';
		return t(`notifications.opens.${kind}`, { target: n.action_target || '' });
	}

	// Per-row expand/collapse for the full detail panel.
	let expandedIds = $state(new Set());
	function toggleExpand(id) {
		const next = new Set(expandedIds);
		if (next.has(id)) next.delete(id); else next.add(id);
		expandedIds = next;
	}

	function scheduledLabel(iso) {
		if (!iso) return '';
		const d = new Date(iso);
		if (isNaN(d.getTime())) return '';
		return t('notifications.scheduledAt', { time: d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) });
	}

	// Group notifications by client
	let groupedByClient = $derived.by(() => {
		const groups = {};
		for (const n of notifications) {
			const cid = n.client_id;
			if (!groups[cid]) groups[cid] = { name: n.client_name || clientName(cid, clients), notifications: [] };
			groups[cid].notifications.push(n);
		}
		return Object.entries(groups).toSorted((a, b) => a[1].name.localeCompare(b[1].name));
	});

	// Show all clients in dropdown (not just active ones - admin may want to queue notifications).
	// Watches are left out: they show the notifications of their phone.
	let sortedClients = $derived(
		clients.filter((c) => !isWatch(c)).toSorted((a, b) => (a.name || '').localeCompare(b.name || ''))
	);

	async function setHistory(v) {
		showHistory = v === 'all';
		await loadData();
	}
</script>

<div class="page-narrow">
	<PageHeader title={t('notifications.title')} subtitle={t('notifications.subtitle')}>
		{#snippet actions()}
			<Segmented label={t('notifications.view')} options={[{ value: 'active', label: t('notifications.activeOnly') }, { value: 'all', label: t('notifications.showHistory') }]} value={showHistory ? 'all' : 'active'} onchange={setHistory} />
			<button type="button" class="btn btn-primary" onclick={() => (showSendForm = true)}><Icon name="send" />{t('notifications.compose')}</button>
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if loadError && notifications.length === 0}
		<div class="card"><ErrorState message={loadError} onretry={loadData} /></div>
	{:else if notifications.length === 0}
		<div class="card">
			<EmptyState icon="bell" title={t('notifications.emptyTitle')} text={showHistory ? t('notifications.emptyHistory') : t('notifications.emptyActive')}>
				<button type="button" class="btn btn-primary" onclick={() => (showSendForm = true)}><Icon name="send" />{t('notifications.compose')}</button>
			</EmptyState>
		</div>
	{:else}
		<div class="stack">
			{#each groupedByClient as [clientId, group] (clientId)}
				<section class="card card-flush" aria-labelledby="nh-{clientId}">
					<header class="nh">
						<h2 id="nh-{clientId}" class="t-card flex-1"><a class="plain" href="{base}/clients/{clientId}">{group.name}</a></h2>
						<span class="badge">{t('notifications.count', { count: group.notifications.length })}</span>
						<button type="button" class="btn btn-sm btn-danger" onclick={() => askClearClient(clientId, group.name)} disabled={clearingClientId === clientId} aria-label={t('notifications.clearAllFor', { name: group.name })}>
							{clearingClientId === clientId ? t('notifications.clearing') : t('notifications.clearAll')}
						</button>
					</header>
					<ul class="nlist">
						{#each group.notifications as n (n.id)}
							{@const st = notifStatus(n)}
							{@const expanded = expandedIds.has(n.id)}
							<li>
								<div class="nrow">
									<span class="sw sw-{COLORS.includes(n.color) ? n.color : 'none'}" aria-hidden="true"></span>
									<div class="flex-1 min-w-0">
										<div class="row gap-1.5">
											<b class="font-semibold">{n.title || '–'}</b>
											{#if canonicalType(n.notification_type) === 'sticky'}<span class="badge badge-warn">{t('notifications.types.sticky')}</span>{/if}
											{#if displayMode(n) !== 'none'}<span class="badge">{t(`notifications.displays.${displayMode(n)}`)}</span>{/if}
											{#if n.scheduled_at}<span class="badge badge-info" title={formatDateTime(n.scheduled_at)}>{scheduledLabel(n.scheduled_at)}</span>{/if}
											<span class="badge {st.tone ? `badge-${st.tone}` : ''}" title={st.hint || undefined}><span class="dot"></span>{st.label}</span>
										</div>
										{#if n.message}<p class="nmsg" class:clamp={!expanded}>{n.message}</p>{/if}
										<div class="row mt-1">
											<span class="t-help">{formatDateTime(n.created_at)}</span>
											<button type="button" class="btn btn-ghost btn-sm" onclick={() => toggleExpand(n.id)} aria-expanded={expanded ? 'true' : 'false'} aria-controls="nd-{n.id}">
												{expanded ? t('notifications.hideDetails') : t('notifications.showDetails')}<Icon name="chevdown" class={expanded ? 'rot' : ''} />
											</button>
										</div>
									</div>
									<button type="button" class="btn btn-ghost btn-icon btn-sm" onclick={() => askDelete(n)} disabled={deletingId === n.id} aria-label={t('notifications.deleteOne')}><Icon name="trash" /></button>
								</div>
								{#if expanded}
									<dl class="dl box nd" id="nd-{n.id}">
										<dt>{t('notifications.type')}</dt><dd>{t(`notifications.types.${canonicalType(n.notification_type)}`)}</dd>
										<dt>{t('notifications.display')}</dt><dd>{t(`notifications.displays.${displayMode(n)}`)}</dd>
										<dt>{t('notifications.priority')}</dt><dd>{n.priority}</dd>
										<dt>{t('notifications.color')}</dt><dd>{t(`notifications.colors.${COLORS.includes(n.color) ? n.color : 'none'}`)}</dd>
										{#if n.icon}<dt>{t('notifications.icon')}</dt><dd class="t-mono">{n.icon}</dd>{/if}
										<dt>{t('notifications.onTap')}</dt><dd>{actionLabel(n)}</dd>
										<dt>{t('notifications.status')}</dt><dd>{st.label}{#if st.hint}<small class="block t-help">{st.hint}</small>{/if}</dd>
										{#if n.scheduled_at}<dt>{t('notifications.scheduled')}</dt><dd>{formatDateTime(n.scheduled_at)}</dd>{/if}
										<dt>{t('notifications.created')}</dt><dd>{formatDateTime(n.created_at)}</dd>
										{#if n.delivered_at}<dt>{t('notifications.delivered')}</dt><dd>{formatDateTime(n.delivered_at)}</dd>{/if}
										{#if n.dismissed_at}<dt>{t('notifications.dismissed')}</dt><dd>{formatDateTime(n.dismissed_at)}</dd>{/if}
										{#if n.message}<dt>{t('notifications.message')}</dt><dd class="whitespace-pre-wrap">{n.message}</dd>{/if}
									</dl>
								{/if}
							</li>
						{/each}
					</ul>
				</section>
			{/each}
		</div>
	{/if}
</div>

<Modal bind:open={showSendForm} title={t('notifications.composeTitle')} description={t('notifications.composeHint')} size="lg" dismissible={!sending}>
	<form id="compose-form" class="stack compose" onsubmit={(e) => { e.preventDefault(); handleSend(); }} novalidate>
		<Field label={t('notifications.recipient')} required error={sendErrors.client} hint={sendClientId === 'all' ? t('notifications.broadcastHint') : ''} id="n-recipient">
			{#snippet children({ id, describedby, invalid })}
				<select {id} class="select" required aria-describedby={describedby} aria-invalid={invalid} bind:value={sendClientId} onchange={() => sendErrors.client && sendClientId && (sendErrors = { ...sendErrors, client: '' })}>
					<option value="">{t('notifications.selectClient')}</option>
					<option value="all">{t('notifications.broadcast')}</option>
					{#each sortedClients as c (c.id)}<option value={c.id}>{c.name}</option>{/each}
				</select>
			{/snippet}
		</Field>

		<fieldset class="stack">
			<legend class="t-overline">{t('notifications.content')}</legend>
			<Field label={t('notifications.titleLabel')} required error={sendErrors.title} id="n-title">
				{#snippet children({ id, describedby, invalid })}
					<input {id} class="input" required aria-describedby={describedby} aria-invalid={invalid} bind:value={sendTitle} oninput={() => sendErrors.title && sendTitle.trim() && (sendErrors = { ...sendErrors, title: '' })} placeholder={t('notifications.titlePlaceholder')} />
				{/snippet}
			</Field>
			<Field label="{t('notifications.message')} ({t('common.optional')})" id="n-message">
				{#snippet children({ id })}<textarea {id} class="textarea" rows="3" bind:value={sendMessage} placeholder={t('notifications.messagePlaceholder')}></textarea>{/snippet}
			</Field>
		</fieldset>

		<fieldset class="stack">
			<legend class="t-overline">{t('notifications.appearance')}</legend>
			<Field label={t('notifications.icon')} id="n-icon">
				{#snippet children({ id })}<IconPicker {id} value={sendIcon} onchange={(v) => (sendIcon = v)} />{/snippet}
			</Field>
			<div class="field">
				<span class="field-label">{t('notifications.color')}</span>
				<div class="row" role="group" aria-label={t('notifications.color')}>
					{#each COLORS as c (c)}
						<button type="button" class="chip" aria-pressed={sendColor === c ? 'true' : 'false'} onclick={() => (sendColor = c)}>
							<span class="sw sw-{c}" aria-hidden="true"></span>{t(`notifications.colors.${c}`)}
						</button>
					{/each}
				</div>
			</div>
			<div class="field">
				<span class="field-label">{t('notifications.type')}</span>
				<Segmented label={t('notifications.type')} options={TYPES.map((v) => ({ value: v, label: t(`notifications.types.${v}`) }))} bind:value={sendType} />
				<p class="t-help">{t(`notifications.typeHint.${sendType}`)}</p>
			</div>
			<Switch bind:checked={sendShowTime} label={t('notifications.showTime')} description={t('notifications.showTimeHint')} />
		</fieldset>

		<fieldset class="stack">
			<legend class="t-overline">{t('notifications.attention')}</legend>
			<div class="field">
				<span class="field-label">{t('notifications.display')}</span>
				<Segmented label={t('notifications.display')} options={DISPLAYS.map((v) => ({ value: v, label: t(`notifications.displays.${v}`) }))} value={sendPopupMode} onchange={onDisplayPick} />
				<p class="t-help">{sendPopupMode === 'direct' ? t('notifications.directHint') : t('notifications.displayHint')}</p>
			</div>
			{#if directNeedsAction}<Banner tone="error">{t('notifications.directNeedsLink')}</Banner>{/if}
			<div class="field">
				<label class="field-label" for="n-priority">{t('notifications.priority')}</label>
				<div class="row flex-nowrap">
					<button type="button" class="btn btn-icon" onclick={() => stepPriority(-1)} disabled={sendPriority <= 0} aria-label={t('notifications.priorityDown')}><Icon name="minus" /></button>
					<input id="n-priority" class="input w-24 text-center" type="number" min="0" max="100" step="1" value={sendPriority} oninput={(e) => (sendPriority = clampPriority(e.currentTarget.value))} onchange={(e) => (e.currentTarget.value = String(sendPriority))} aria-describedby="n-priority-hint" />
					<button type="button" class="btn btn-icon" onclick={() => stepPriority(1)} disabled={sendPriority >= 100} aria-label={t('notifications.priorityUp')}><Icon name="plus" /></button>
				</div>
				<p id="n-priority-hint" class="t-help">{t('notifications.priorityHint')}</p>
			</div>
		</fieldset>

		<fieldset class="stack" bind:this={linkSectionEl}>
			<legend class="t-overline">{t('notifications.link')}</legend>
			<p class="t-help">{sendPopupMode === 'direct' ? t('notifications.linkHintDirect') : t('notifications.linkHint')}</p>
			<div class="grid sm:grid-cols-2 gap-3">
				<Field label={t('notifications.onTap')} id="n-action">
					{#snippet children({ id })}
						<select {id} class="select" value={sendActionType} onchange={(e) => onActionTypeChange(e.currentTarget.value)}>
							{#each ['none', 'area', 'entity', 'url'] as a (a)}<option value={a}>{t(`notifications.actions.${a}`)}</option>{/each}
						</select>
					{/snippet}
				</Field>
				{#if sendActionType === 'area'}
					<Field label={t('notifications.area')} id="n-area">
						{#snippet children({ id })}
							{#if addonAreas.length}
								<select {id} class="select" bind:value={sendActionTarget}>
									<option value="">{t('notifications.selectArea')}</option>
									<option value="__home__">{t('notifications.homeTarget')}</option>
									{#each addonAreas as a (a.id)}<option value={a.id}>{a.name || a.id}</option>{/each}
								</select>
							{:else}
								<input {id} class="input" bind:value={sendActionTarget} placeholder="area_id" />
							{/if}
						{/snippet}
					</Field>
				{:else if sendActionType === 'entity'}
					<div class="field">
						<span class="field-label" id="n-entity-label">{t('notifications.entity')}</span>
						<button type="button" class="btn justify-between" onclick={openEntityPicker} aria-labelledby="n-entity-label n-entity-value">
							<span id="n-entity-value" class="truncate">{sendActionTarget ? sendActionEntityName || sendActionTarget : t('notifications.selectEntity')}</span>
							<Icon name="chevdown" />
						</button>
					</div>
				{:else if sendActionType === 'url'}
					<Field label={t('notifications.url')} id="n-url">
						{#snippet children({ id })}<input {id} class="input" type="url" bind:value={sendActionTarget} placeholder="https://example.com" />{/snippet}
					</Field>
				{/if}
			</div>
			{#if areaNotVisibleToClient}
				<Banner tone="warn">{t('notifications.areaHidden')}</Banner>
			{:else if areaMayBeHiddenForSome}
				<Banner tone="neutral">{t('notifications.areaMaybeHidden')}</Banner>
			{/if}
		</fieldset>

		<fieldset class="stack">
			<legend class="t-overline">{t('notifications.schedule')}</legend>
			<Segmented
				label={t('notifications.schedule')}
				options={[{ value: 'now', label: t('notifications.sendNow') }, { value: 'schedule', label: t('notifications.scheduleMode') }]}
				value={scheduleMode}
				onchange={(v) => {
					scheduleMode = v;
					if (v === 'schedule' && !sendScheduleAt) applyPreset('1h');
				}}
			/>
			{#if scheduleMode === 'schedule'}
				<div class="row" role="group" aria-label={t('notifications.schedule')}>
					{#each SCHEDULE_PRESETS as p (p)}
						<button type="button" class="chip" aria-pressed={schedulePreset === p ? 'true' : 'false'} onclick={() => applyPreset(p)}>{t(`notifications.presets.${p}`)}</button>
					{/each}
				</div>
				{#if schedulePreset === 'custom' || sendScheduleAt}
					<Field label={t('notifications.dateTime')} error={scheduleInPast ? t('notifications.inPast') : ''} id="n-when">
						{#snippet children({ id, describedby, invalid })}
							<input {id} class="input" type="datetime-local" aria-describedby={describedby} aria-invalid={invalid} bind:value={sendScheduleAt} oninput={onPickerInput} />
						{/snippet}
					</Field>
				{/if}
				{#if schedulePreview}
					<Banner tone="info" icon="clock">
						{t('notifications.scheduledFor', { date: schedulePreview })}
						{#snippet actions()}<button type="button" class="btn btn-sm" onclick={clearSchedule}>{t('notifications.clearSchedule')}</button>{/snippet}
					</Banner>
				{/if}
				{#if !scheduleInPast}<p class="t-help">{t('notifications.scheduleHint')}</p>{/if}
			{/if}
		</fieldset>
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={() => { close(); resetForm(); }} disabled={sending}>{t('common.cancel')}</button>
		<button type="submit" form="compose-form" class="btn btn-primary" disabled={!canSend}>
			<Icon name="send" />{sending ? t('notifications.sending') : scheduleMode === 'schedule' ? t('notifications.scheduleSend') : t('notifications.send')}
		</button>
	{/snippet}
</Modal>

<ConfirmDialog bind:open={confirmOpen} title={confirmTitle} message={confirmMessage} {confirmLabel} danger onconfirm={confirmAction} />

<AddEntityModal bind:open={showEntityPicker} sections={[]} bind:sectionId={entityPickerSectionId} entities={haEntities} allEntityIds={noExistingEntities} onadd={onEntityPicked} />

<style>
	.nh {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		border-bottom: 1px solid var(--line);
	}
	.plain {
		color: inherit;
		text-decoration: none;
	}
	.plain:hover {
		text-decoration: underline;
	}
	.nlist {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.nlist > li {
		border-bottom: 1px solid var(--line);
		padding: 10px 14px;
	}
	.nlist > li:last-child {
		border-bottom: 0;
	}
	.nrow {
		display: flex;
		gap: 12px;
		align-items: flex-start;
	}
	.nmsg {
		margin: 4px 0 0;
		color: var(--ink-2);
		overflow-wrap: anywhere;
	}
	.nmsg.clamp {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.nd {
		margin: 8px 0 0 22px;
	}
	.nrow :global(.rot) {
		transform: rotate(180deg);
	}
	.sw {
		display: inline-block;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		flex: none;
		margin-top: 4px;
		border: 1px solid var(--line-2);
	}
	.chip .sw {
		margin-top: 0;
	}
	.sw-none {
		background: transparent;
	}
	.sw-red {
		background: #ef4444;
	}
	.sw-blue {
		background: #3b82f6;
	}
	.sw-green {
		background: #22c55e;
	}
	.sw-yellow {
		background: #eab308;
	}
	.sw-orange {
		background: #f97316;
	}
	.compose fieldset {
		border: 0;
		margin: 0;
		padding: 16px 0 0;
		border-top: 1px solid var(--line);
		min-width: 0;
	}
	.compose legend {
		float: left;
		width: 100%;
		margin-bottom: 4px;
	}
	.compose legend + :global(*) {
		clear: both;
	}
</style>
