<script>
	/**
	 * Guests: time-limited devices with their own HA user and a
	 * profile (never with parental controls); remote access is a profile
	 * rule. Extend (at most 90 days per grant), end access; the defaults for
	 * invitations (guest profile, duration) and the open invitations of
	 * member devices.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { openPairing } from '$lib/stores/pairing.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { debounce, formatDate, formatTimeRemaining, formatWeekdayTime } from '$lib/utils.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Avatar from '$lib/ui/Avatar.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import RowLink from '$lib/ui/RowLink.svelte';
	import DefaultProfilesCard from '$lib/components/profiles/DefaultProfilesCard.svelte';
	import RemoteKeyWarning from '$lib/components/profiles/RemoteKeyWarning.svelte';
	import { previewExtend } from '$lib/profiles/preview.js';
	import Modal from '$lib/ui/Modal.svelte';
	import { remoteState } from '$lib/device-info.js';

	const SOON_MS = 2 * 3600_000;

	let clients = $state([]);
	let loading = $state(true);
	let loadError = $state('');
	let busyId = $state('');

	let profiles = $state([]);

	// Open invitations of member devices
	let invites = $state([]);
	let now = $state(Date.now());

	// End confirm
	let endFor = $state(null);
	let endOpen = $state(false);

	let unsubs = [];
	let clock;
	const refresh = debounce(() => loadGuests(), 250);

	onMount(() => {
		loadGuests();
		loadInvites();
		api.listProfiles().then((r) => (profiles = r?.profiles || [])).catch(() => (profiles = []));
		clock = setInterval(() => (now = Date.now()), 15_000);
		unsubs.push(
			...['client_paired', 'client_kicked', 'guests_expired', 'guest_extended', 'client_connected', 'client_disconnected', 'resync'].map((type) =>
				onBroadcast(type, refresh),
			),
			...['guest_invite_opened', 'guest_invite_closed', 'resync'].map((type) => onBroadcast(type, loadInvites)),
		);
	});

	onDestroy(() => {
		clearInterval(clock);
		refresh.cancel();
		unsubs.forEach((fn) => fn());
	});

	async function loadGuests() {
		try {
			clients = await api.getPairedClients();
			loadError = '';
		} catch (e) {
			loadError = t('guests.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	async function loadInvites() {
		try {
			invites = (await api.getGuestInvites())?.invites || [];
		} catch {
			invites = [];
		}
	}

	async function cancelInvite(inv) {
		try {
			await api.cancelGuestInvite(inv.id);
			addToast(t('guests.inviteCancelled', { device: inv.invited_by?.name || '' }), 'success');
		} catch (e) {
			addToast(t('guests.saveFailed', { message: e.message }), 'error');
		}
		loadInvites();
	}

	function secondsLeft(iso) {
		return Math.max(0, Math.round((Date.parse(iso) - now) / 1000));
	}

	let guests = $derived(
		clients
			.filter((c) => c.access === 'guest' && !c.pending_deletion)
			.toSorted((a, b) => Date.parse(a.expires_at || '9999') - Date.parse(b.expires_at || '9999')),
	);

	function guestName(c) {
		return c.owner_label || c.name;
	}

	function progress(c) {
		if (!c.expires_at || !c.created_at) return 0;
		const start = Date.parse(c.created_at);
		const end = Date.parse(c.expires_at);
		if (!(end > start)) return 0;
		return Math.max(0, Math.min(100, ((end - now) / (end - start)) * 100));
	}

	function expired(c) {
		return !!c.expires_at && Date.parse(c.expires_at) <= now;
	}

	/** One status per card: setting up, expired, expiring soon or active. */
	function status(c) {
		if (expired(c)) return { tone: 'badge-err', label: t('guests.expired'), id: 'expired' };
		if (c.ha_token_status === 'pending') return { tone: 'badge-warn', label: t('guests.settingUp'), id: 'setup', title: t('guests.settingUpTitle') };
		if (c.expires_at && Date.parse(c.expires_at) - now < SOON_MS) return { tone: 'badge-warn', label: t('guests.expiringSoon'), id: 'soon' };
		return { tone: 'badge-guest', label: t('guests.badge'), id: 'active' };
	}

	// "+1 day" asks the hub first (dry run): when the extension leaves the guest on "Hub only" with remote
	// access, now or after a scheduled end, or the hub gives no answer, it asks before extending.
	// One extension at a time: while a dry run is out or the dialog is open, every "+1 day" is disabled,
	// and an answer that is not for the guest asked last is dropped.
	let extendAsk = $state(null);
	let extendAskOpen = $state(false);
	let extendPendingId = $state('');
	let extendLocked = $derived(!!extendPendingId || extendAskOpen);

	async function extendDay(c) {
		if (extendLocked) return;
		extendPendingId = c.id;
		busyId = c.id;
		try {
			const data = { hours: 24 };
			const pv = await previewExtend(c.id, data);
			if (extendPendingId !== c.id || extendAskOpen) return;
			if (pv.now || pv.at || pv.unknown) {
				extendAsk = { c, data, pv };
				extendAskOpen = true;
				return;
			}
			await doExtend(c, data);
		} finally {
			if (extendPendingId === c.id) extendPendingId = '';
			busyId = '';
		}
	}

	async function doExtend(c, data) {
		try {
			const r = await api.extendGuest(c.id, data);
			addToast(t('guests.extended', { date: formatWeekdayTime(r?.expires_at) }), 'success');
			await loadGuests();
		} catch (e) {
			addToast(t('guests.extendFailed', { message: e.message }), 'error');
		}
	}

	async function confirmExtend() {
		const ask = extendAsk;
		extendAskOpen = false;
		if (!ask) return;
		busyId = ask.c.id;
		try {
			await doExtend(ask.c, ask.data);
		} finally {
			busyId = '';
		}
	}

	function askEnd(c) {
		endFor = c;
		endOpen = true;
	}

	async function endAccess() {
		const c = endFor;
		if (!c) return;
		busyId = c.id;
		try {
			await api.kickClient(c.id);
			addToast(t('guests.ended', { name: guestName(c) }), 'success');
			await loadGuests();
		} catch (e) {
			addToast(t('guests.endFailed', { message: e.message }), 'error');
		} finally {
			busyId = '';
		}
	}

</script>

<div class="page">
	<PageHeader title={t('guests.title')} subtitle={t('guests.subtitle')}>
		{#snippet actions()}
			<button type="button" class="btn btn-primary" onclick={() => openPairing('guest')} data-testid="open-pairing-guest"><Icon name="plus" />{t('guests.invite')}</button>
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if loadError && !clients.length}
		<div class="card"><ErrorState message={loadError} onretry={loadGuests} /></div>
	{:else if guests.length === 0}
		<div class="card">
			<EmptyState icon="users" title={t('guests.emptyTitle')} text={t('guests.emptyText')}>
				<button type="button" class="btn btn-primary" onclick={() => openPairing('guest')}><Icon name="plus" />{t('guests.invite')}</button>
			</EmptyState>
		</div>
	{:else}
		<div class="glist" data-testid="guest-list">
			{#each guests as c (c.id)}
				{@const st = status(c)}
				<article class="card gcard rowlink" class:is-expired={st.id === 'expired'} aria-labelledby="g-{c.id}" data-testid="guest-card" data-status={st.id}>
					<div class="gh">
						<Avatar name={guestName(c)} />
						<span class="min-w-0 flex-1">
							<h2 id="g-{c.id}" class="t-card truncate"><RowLink href="{base}/clients/{c.id}" testid="guest-open">{guestName(c)}</RowLink></h2>
							<small class="t-help block truncate">{t('guests.since', { device: c.name, date: formatDate(c.created_at) })}</small>
							{#if c.set_up_by}
								<small class="t-help block truncate" data-testid="guest-invited-by">{t(c.set_up_by.method === 'invite' ? 'guests.invitedBy' : 'guests.setUpBy', { device: c.set_up_by.name })}</small>
							{/if}
						</span>
						<span class="badge {st.tone} flex-none" title={st.title} data-testid="guest-status">{st.label}</span>
						<span class="rowlink-chev" aria-hidden="true"><Icon name="chev" /></span>
					</div>
					{#if c.expires_at}
						<div class="progress exp exp-{st.id}" role="progressbar" aria-label={t('guests.remaining')} aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(progress(c))} aria-valuetext={formatTimeRemaining(c.expires_at)}>
							<i style:width="{progress(c)}%"></i>
						</div>
						<p class="t-help">
							{expired(c) ? t('guests.expiredAt', { date: formatWeekdayTime(c.expires_at) }) : t('guests.expiresAt', { date: formatWeekdayTime(c.expires_at) })}
							· {formatTimeRemaining(c.expires_at)}
						</p>
					{/if}
					<div class="row" data-testid="guest-profile">
						<span class="badge"><Icon name="template" />{c.profile_name}</span>
						<span class="badge" class:badge-info={remoteState(c).id === 'on'} class:badge-warn={remoteState(c).id === 'unknown'} data-testid="guest-remote" data-state={remoteState(c).id}><Icon name={remoteState(c).icon} />{remoteState(c).label}</span>
						{#if c.override_count}<span class="badge badge-warn"><Icon name="sliders" />{t('profiles.adjustedCount', { count: c.override_count })}</span>{/if}
					</div>
					<div class="row gactions rowlink-ctl" data-eq="guest-actions">
						<button type="button" class="btn btn-sm" onclick={() => extendDay(c)} disabled={busyId === c.id || extendLocked} aria-label={t('guests.extendDayLabel', { name: guestName(c) })}><Icon name="plus" />{t('guests.extendDay')}</button>
						<a class="btn btn-sm" href="{base}/clients/{c.id}" aria-label={t('guests.rulesLabel', { name: guestName(c) })}><Icon name="sliders" />{t('guests.rules')}</a>
						<button type="button" class="btn btn-sm btn-danger" onclick={() => askEnd(c)} disabled={busyId === c.id} aria-label={t('guests.endLabel', { name: guestName(c) })}>{t('guests.end')}</button>
					</div>
				</article>
			{/each}
		</div>
	{/if}

	<div class="two">
		<div data-testid="guest-defaults">
			<DefaultProfilesCard {profiles} compact />
		</div>
		<div class="stack">
			<section class="card" aria-labelledby="ginv-h" data-testid="guest-invites">
				<div class="card-head"><Icon name="qr" /><h2 id="ginv-h">{t('guests.invitesTitle')}</h2></div>
				<p class="t-help mb-3">{t('guests.invitesHint')}</p>
				{#if invites.length}
					<ul class="inv-list">
						{#each invites as inv (inv.id)}
							<li class="inv" data-testid="guest-invite">
								<span class="min-w-0 flex-1">
									<span class="block truncate">{t('guests.inviteBy', { device: inv.invited_by?.name || '' })}</span>
									<small class="t-help">{t('guests.inviteLeft', { time: formatTimeRemaining(inv.expires_at) })}</small>
								</span>
								<button type="button" class="btn btn-sm" onclick={() => cancelInvite(inv)} aria-label={t('guests.inviteCancelLabel', { device: inv.invited_by?.name || '' })} disabled={secondsLeft(inv.expires_at) === 0}>{t('guests.inviteCancel')}</button>
							</li>
						{/each}
					</ul>
				{:else}
					<p class="t-help" data-testid="guest-invites-empty">{t('guests.invitesEmpty')}</p>
				{/if}
			</section>
		<section class="card" aria-labelledby="gnote-h">
			<div class="card-head"><Icon name="info" /><h2 id="gnote-h">{t('guests.noteTitle')}</h2></div>
			<p class="t-help">{t('guests.note')}</p>
		</section>
		</div>
	</div>
</div>

<Modal bind:open={extendAskOpen} title={t('guests.extendAskTitle', { name: extendAsk ? guestName(extendAsk.c) : '' })} size="md" testid="guest-extend-dialog">
	{#if extendAsk}
		<div class="stack">
			{#if extendAsk.pv.expires_at}<p>{t('guests.extendAskUntil', { date: formatWeekdayTime(extendAsk.pv.expires_at) })}</p>{/if}
			{#if extendAsk.pv.unknown}<p class="t-help" data-testid="guest-extend-unknown">{t('guests.extendUnknown')}</p>{/if}
			<RemoteKeyWarning from={!extendAsk.pv.now && extendAsk.pv.at ? extendAsk.pv.at : null} testid="guest-extend-remote-key-warning" />
		</div>
	{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close}>{t('common.cancel')}</button>
			<button type="button" class="btn btn-primary" onclick={confirmExtend} data-testid="guest-extend-confirm">{t('guests.extendDay')}</button>
		</div>
	{/snippet}
</Modal>
<ConfirmDialog
	bind:open={endOpen}
	title={t('guests.endTitle')}
	message={endFor ? t('guests.endMessage', { name: guestName(endFor) }) : ''}
	confirmLabel={t('guests.end')}
	danger
	onconfirm={endAccess}
/>

<style>
	.glist {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr));
		gap: 14px;
	}
	/* minmax(0, 1fr): long names truncate instead of widening the card,
	   so badge and expiry bar stay inside the card edge. */
	.gcard {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 10px;
		min-width: 0;
	}
	.gh {
		display: flex;
		gap: 12px;
		align-items: center;
	}
	.gh :global(.avatar) {
		width: 36px;
		height: 36px;
		font-size: 14px;
		background: var(--accent-ink);
	}
	.exp > i {
		background: var(--info);
	}
	.exp-soon > i {
		background: var(--warn);
	}
	.gcard.is-expired .gh :global(.avatar) {
		background: var(--ink-3);
	}
	/* Equal sizes: three equal buttons. */
	.gactions {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 8px;
	}
	.gactions > :global(.btn) {
		justify-content: center;
		min-width: 0;
	}
	.two {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 14px;
		margin-top: 14px;
	}
	.stack {
		display: grid;
		gap: 14px;
		align-content: start;
		min-width: 0;
	}
	.inv-list {
		display: grid;
		gap: 8px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.inv {
		display: flex;
		gap: 10px;
		align-items: center;
		min-width: 0;
	}
	@media (max-width: 767px) {
		.two {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
