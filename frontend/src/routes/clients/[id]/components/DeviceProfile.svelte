<script>
	/**
	 * Profile of a device: profile chip with "Choose profile"
	 * (preview), access (member / guest until …), own area, the summary
	 * sentence, kids status with parent release (parental controls), remote
	 * PIN (from the profile or the device's own) and the effective rules
	 * with adjust/reset. "Reset everything to the profile" and "Create a
	 * profile from this device" at the end.
	 */
	import { onMount, untrack } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { formatTimeRemaining, formatWeekdayTime } from '$lib/utils.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import LoadError from '$lib/components/LoadError.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ProfileIcon from '$lib/components/profiles/ProfileIcon.svelte';
	import AssignProfileDialog from '$lib/components/profiles/AssignProfileDialog.svelte';
	import AdjustDialog from '$lib/components/profiles/AdjustDialog.svelte';
	import NewProfileDialog from '$lib/components/profiles/NewProfileDialog.svelte';
	import RemotePinDialog from '$lib/components/profiles/RemotePinDialog.svelte';
	import PreviewView from '$lib/components/profiles/PreviewView.svelte';
	import LooseningWarning from '$lib/components/profiles/LooseningWarning.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import { endsHubRemote } from '$lib/profiles/preview.js';
	import EffectiveRules from './EffectiveRules.svelte';
	import FamilyCard from './FamilyCard.svelte';
	import ProfileHistory from '$lib/components/profiles/ProfileHistory.svelte';
	import { loadCatalog } from '$lib/profiles/catalog.svelte.js';
	import { loadRuleContext, areaNamer } from '$lib/profiles/context.js';
	import { effectiveSummary } from '$lib/profiles/format.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { OVERRIDE_MINUTES } from '$lib/data/kids.js';

	let { client, onchanged = null, onextend = null, oneffective = null } = $props();

	let effective = $state(null);
	let error = $state('');
	let profiles = $state([]);
	let ref = $state({ areas: [], tags: [], entities: [] });
	let kids = $state(null);
	let busy = $state(false);
	let busyRule = $state('');
	let historyTick = $state(0);

	let assignOpen = $state(false);
	let resetOpen = $state(false);
	let adjustOpen = $state(false);
	let adjustKey = $state('');
	let newOpen = $state(false);
	let newMode = $state('device');
	let pinOpen = $state(false);

	onMount(() => {
		loadRuleContext().then((r) => (ref = r));
		api.listProfiles().then((r) => (profiles = r?.profiles || [])).catch(() => {});
		const offs = [
			onBroadcast('client_config_updated', (d) => {
				if (!d?.client_id || d.client_id === client?.id) load();
			}),
			onBroadcast('profiles_changed', (d) => {
				if (d?.client_ids?.includes(client?.id) || d?.profile_ids?.includes(effective?.profile?.id)) load();
			}),
			onBroadcast('access_requests_changed', (d) => {
				if (d?.client_ids?.includes(client?.id)) {
					load();
					loadRequest();
				}
			}),
			onBroadcast('device_activity', (d) => {
				if (d?.client_id === client?.id && ['rule_blocked', 'kids_override'].includes(d?.event)) loadKids();
			}),
		];
		return () => offs.forEach((off) => off());
	});

	// The page shows the Home Assistant access from the effective rules.
	$effect(() => {
		oneffective?.(effective);
	});

	let cid = $derived(client?.id);
	$effect(() => {
		if (cid) untrack(load);
		if (cid) untrack(loadRequest);
	});

	// Open early access request of this device (answer here like a recipient).
	let accessRequest = $state(null);
	async function loadRequest() {
		try {
			const r = await api.listAccessRequests();
			accessRequest = (r?.requests || []).find((x) => x.client_id === client?.id && x.status === 'open') || null;
		} catch {
			accessRequest = null;
		}
	}

	async function load() {
		try {
			const [eff] = await Promise.all([api.getClientEffective(client.id), loadCatalog()]);
			if (!eff?.rules || !eff?.profile) throw new Error(t('profiles.device.badAnswer'));
			effective = eff;
			error = '';
			historyTick += 1;
			if (eff?.profile?.child_protection) loadKids();
			else kids = null;
		} catch (e) {
			error = e.message;
		}
	}

	/** A child's watch follows its phone's kids rules and state; releases
	 * and pauses are the phone's. */
	let childWatchPhone = $derived(
		['watchos', 'wearos'].includes(client?.platform) && client?.child_protection && client?.set_up_by?.client_id ? client.set_up_by : null
	);

	async function loadKids() {
		if (childWatchPhone) {
			kids = null;
			return;
		}
		try {
			kids = await api.getKidsStatus(client.id);
		} catch {
			kids = null;
		}
	}

	async function refreshClient() {
		try {
			onchanged?.(await api.getClient(client.id));
		} catch {
			/* the page refreshes on its own */
		}
	}

	let ctx = $derived({
		// A watch limited by its phone (reason limited_by_phone) names the phone.
		phoneName: effective?.access?.set_up_by?.name || client?.set_up_by?.name || '',
		...ref,
		areaName: areaNamer(ref.areas),
		childProtection: !!effective?.profile?.child_protection,
		ownAreaName: effective?.own_area_id ? areaNamer(ref.areas)(effective.own_area_id) : '',
	});

	let isGuest = $derived(effective?.access?.kind === 'guest' || client?.access === 'guest');
	let expired = $derived(isGuest && !!client?.expires_at && Date.parse(client.expires_at) <= Date.now());
	// remote_pin_source = profile | device | null; the profile's checkbox "Fixed" blocks a device PIN.
	let pin = $derived({
		source: effective?.remote_pin_source ?? null,
		fixed: !!profiles.find((p) => p.id === effective?.profile?.id)?.remote_pin_fixed,
	});

	async function setOwnArea(e) {
		const areaId = e.currentTarget.value || null;
		busy = true;
		try {
			effective = await api.setClientOwnArea(client.id, areaId);
			historyTick += 1;
			addToast(areaId ? t('profiles.device.ownAreaSaved', { name: areaNamer(ref.areas)(areaId) }) : t('profiles.device.ownAreaRemoved'), 'success');
			refreshClient();
		} catch (err) {
			addToast(errorText(err), 'error');
		} finally {
			busy = false;
		}
	}

	function adjust(key) {
		adjustKey = key;
		adjustOpen = true;
	}

	// Reset of one adjustment: a preview first. When it loosens a rule or leaves the device on
	// "Hub only" with remote access, the preview (with the warning) is shown before anything is saved.
	let resetKey = $state('');
	let resetPreview = $state(null);
	let resetConfirmed = $state(false);
	let resetPreviewOpen = $state(false);

	async function resetRule(key) {
		busyRule = key;
		try {
			const pv = await api.removeClientOverride(client.id, key, { dryRun: true });
			const risky = !!pv?.loosened_rules?.length || (pv?.devices || []).some((d) => d.client_id === client.id && endsHubRemote(d, client));
			if (risky) {
				resetKey = key;
				resetPreview = pv;
				resetConfirmed = false;
				resetPreviewOpen = true;
				return;
			}
			await doReset(key);
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			busyRule = '';
		}
	}

	async function doReset(key) {
		effective = await api.removeClientOverride(client.id, key);
		historyTick += 1;
		addToast(t('profiles.device.resetDone'), 'success');
		refreshClient();
	}

	async function confirmReset() {
		busyRule = resetKey;
		try {
			await doReset(resetKey);
			resetPreviewOpen = false;
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			busyRule = '';
		}
	}

	function onAdjusted(eff) {
		if (eff?.rules) {
			effective = eff;
			historyTick += 1;
		} else load();
		refreshClient();
	}

	function onAssigned(updated) {
		if (updated?.id) onchanged?.(updated);
		else refreshClient();
		load();
	}

	async function savePin(p) {
		effective = await api.setClientRemotePin(client.id, p);
		addToast(t('profiles.pin.deviceSaved'), 'success');
	}

	async function removePin() {
		busy = true;
		try {
			effective = await api.setClientRemotePin(client.id, null);
			addToast(t('profiles.pin.deviceRemoved'), 'success');
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			busy = false;
		}
	}

	async function release(minutes) {
		busy = true;
		try {
			kids = await api.startKidsOverride(client.id, minutes);
			addToast(t('kids.device.released', { time: formatWeekdayTime(kids.override_until) }), 'success');
		} catch (e) {
			addToast(t('kids.device.actionFailed', { message: e.message }), 'error');
		} finally {
			busy = false;
		}
	}

	async function lift() {
		busy = true;
		try {
			kids = await api.endKidsOverride(client.id);
			addToast(t('kids.device.lifted'), 'success');
		} catch (e) {
			addToast(t('kids.device.actionFailed', { message: e.message }), 'error');
		} finally {
			busy = false;
		}
	}

	let hours = $derived(effective?.access?.hours || null);

	function accessText(h) {
		if (h.open) return h.until ? t('profiles.accessHours.openUntil', { time: formatWeekdayTime(h.until) }) : t('profiles.accessHours.open');
		if (h.reason === 'switch') return t('profiles.accessHours.closedSwitch');
		return h.opens_at ? t('profiles.accessHours.closedUntil', { time: formatWeekdayTime(h.opens_at) }) : t('profiles.accessHours.closed');
	}

	async function setSwitch(on) {
		busy = true;
		try {
			await api.setAccessSwitch(client.id, on);
			addToast(t('profiles.accessHours.switched'), 'success');
			await load();
			refreshClient();
			return true;
		} catch (e) {
			addToast(t('profiles.accessHours.failed', { message: e.message }), 'error');
			return false;
		} finally {
			busy = false;
		}
	}

	function durationText(d, req) {
		return t(`profiles.accessHours.durations.${d}`, { time: req?.opens_at ? formatWeekdayTime(req.opens_at) : '' });
	}

	async function answerRequest(answer, duration = null) {
		if (!accessRequest) return;
		busy = true;
		try {
			await api.answerAccessRequest(accessRequest.request_id, duration ? { answer, duration } : { answer });
			addToast(t('profiles.accessHours.answered'), 'success');
			await Promise.all([load(), loadRequest()]);
			refreshClient();
		} catch (e) {
			addToast(t('profiles.accessHours.answerFailed', { message: e.message }), 'error');
			loadRequest();
		} finally {
			busy = false;
		}
	}

	async function endEarly() {
		busy = true;
		try {
			await api.endEarlyAccess(client.id);
			addToast(t('profiles.accessHours.earlyEnded'), 'success');
			await load();
			refreshClient();
		} catch (e) {
			addToast(t('profiles.accessHours.failed', { message: e.message }), 'error');
		} finally {
			busy = false;
		}
	}

	function onCreated(p) {
		addToast(t('profiles.device.createdFrom', { name: p.name }), 'success');
		load();
		refreshClient();
		api.listProfiles().then((r) => (profiles = r?.profiles || [])).catch(() => {});
	}
</script>

{#if error && !effective?.rules}
	<section class="card"><LoadError message={error} onretry={load} /></section>
{:else if !effective?.rules || !effective?.profile}
	<section class="card"><LoadingState /></section>
{:else}
	<section class="card" aria-labelledby="dprof-h" data-testid="device-profile">
		<h2 id="dprof-h" class="sr-only">{t('profiles.device.profileTitle')}</h2>
		<div class="pgrid">
			<div class="pcol">
				<span class="t-label">{t('profiles.device.profile')}</span>
				<div class="chip-row">
					<a class="pchip" href="{base}/profiles/{effective.profile.id}" data-testid="device-profile-chip">
						<ProfileIcon icon={profiles.find((p) => p.id === effective.profile.id)?.icon} color={profiles.find((p) => p.id === effective.profile.id)?.color} size={26} />
						<span class="truncate">{effective.profile.name}</span>
					</a>
					{#if effective.profile.child_protection}<span class="badge badge-accent"><Icon name="child" />{t('profiles.childProtection')}</span>{/if}
				</div>
				<button type="button" class="btn btn-sm" onclick={() => (assignOpen = true)} disabled={!client.is_active} data-testid="device-choose-profile"><Icon name="template" />{t('profiles.device.choose')}</button>
			</div>
			<div class="pcol">
				<span class="t-label">{t('profiles.device.access')}</span>
				{#if isGuest}
					<span class="badge badge-guest" data-testid="device-access">{expired ? t('devices.expired') : t('profiles.device.guestUntil', { date: formatWeekdayTime(client.expires_at) })}</span>
					{#if !expired}<span class="t-help">{t('time.left', { time: formatTimeRemaining(client.expires_at) })}</span>{/if}
					{#if client.set_up_by}<span class="t-help" data-testid="device-set-up-by">{t(client.set_up_by.method === 'invite' ? 'devices.setUpByInvite' : 'devices.setUpBy')} <a href="{base}/clients/{client.set_up_by.client_id}">{client.set_up_by.name}</a></span>{/if}
					{#if !expired && onextend}<button type="button" class="btn btn-sm" onclick={onextend} data-testid="device-extend"><Icon name="plus" />{t('devices.extend')}</button>{/if}
				{:else}
					<span class="badge" data-testid="device-access"><Icon name="user" />{t('profiles.access.member')}</span>
				{/if}
				<span class="t-help">{t('profiles.device.accessHint')}</span>
			</div>
			<div class="pcol">
				<label class="t-label" for="own-area">{t('profiles.device.ownArea')}</label>
				<select id="own-area" class="select" value={effective.own_area_id || ''} onchange={setOwnArea} disabled={busy} data-testid="own-area">
					<option value="">{t('profiles.device.noOwnArea')}</option>
					{#each ref.areas as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
				</select>
				<span class="t-help">{t('profiles.device.ownAreaHint')}</span>
			</div>
		</div>
		<p class="box mt-3" data-testid="device-summary"><b>{t('profiles.device.inShort')}</b> {effectiveSummary(effective, ctx)}</p>
	</section>

	{#if hours && (hours.schedule || hours.switch !== null)}
		<section class="card" aria-labelledby="access-h" data-testid="device-access-now">
			<div class="card-head"><Icon name="clock" /><h2 id="access-h" class="flex-1">{t('profiles.accessHours.now')}</h2>
				<span class="badge" class:badge-ok={hours.open} class:badge-warn={!hours.open} data-testid="device-access-state">{accessText(hours)}</span>
			</div>
			{#if hours.early}
				<div class="early" data-testid="device-access-early">
					<span class="t-help">{t('profiles.accessHours.earlyBy', { name: hours.early.by?.name || '' })}</span>
					<button type="button" class="btn btn-sm" onclick={endEarly} disabled={busy} data-testid="device-access-early-end"><Icon name="x" />{t('profiles.accessHours.earlyEnd')}</button>
				</div>
			{/if}
			{#if accessRequest}
				<div class="box areq" data-testid="device-access-request">
					<b>{t('profiles.accessHours.requestTitle')}</b>
					<span class="t-help">{accessRequest.opens_at
							? t('profiles.accessHours.requestSince', { time: formatWeekdayTime(accessRequest.created_at), start: formatWeekdayTime(accessRequest.opens_at) })
							: t('profiles.accessHours.requestSinceNoStart', { time: formatWeekdayTime(accessRequest.created_at) })}</span>
					{#if accessRequest.note}<span class="note">„{accessRequest.note}“</span>{/if}
					{#if accessRequest.recipients?.length}<span class="t-help">{t('profiles.accessHours.requestRecipients', { names: accessRequest.recipients.map((r) => r.name).join(', ') })}</span>{/if}
					<div class="rel">
						{#each accessRequest.durations as d (d)}
							<button type="button" class="btn btn-sm btn-primary" disabled={busy} onclick={() => answerRequest('yes', d)} data-testid="device-access-yes-{d}">{t('profiles.accessHours.answerYes', { duration: durationText(d, accessRequest) })}</button>
						{/each}
						<button type="button" class="btn btn-sm" disabled={busy} onclick={() => answerRequest('no')} data-testid="device-access-no">{t('profiles.accessHours.answerNo')}</button>
					</div>
				</div>
			{/if}
			{#if hours.switch !== null}
				<Switch checked={hours.switch} label={t('profiles.accessHours.switch')} disabled={busy || !client.is_active} onchange={setSwitch} testid="device-access-switch" />
				<p class="t-help mt-1">{t('profiles.accessHours.switchHint')}</p>
			{:else}
				<p class="t-help">{t('profiles.accessHours.noSwitchHint')}</p>
			{/if}
		</section>
	{/if}

	{#if effective.profile.child_protection && childWatchPhone}
		<section class="card" aria-labelledby="kids-h" data-testid="kids-watch-follows">
			<div class="card-head"><Icon name="child" /><h2 id="kids-h" class="flex-1">{t('profiles.device.kidsNow')}</h2></div>
			<p class="t-help">{t('kids.device.watchFollows', { name: childWatchPhone.name })}</p>
			<a class="btn btn-sm mt-2" href="{base}/clients/{childWatchPhone.client_id}" data-testid="kids-watch-phone">{t('kids.device.openPhone', { name: childWatchPhone.name })}</a>
		</section>
	{:else if effective.profile.child_protection}
		<section class="card" aria-labelledby="kids-h" data-testid="kids-status">
			<div class="card-head"><Icon name="child" /><h2 id="kids-h" class="flex-1">{t('profiles.device.kidsNow')}</h2>
				{#if kids?.override_until}<span class="badge badge-ok" data-testid="kids-released"><Icon name="clock" />{t('kids.device.releasedUntil', { time: formatWeekdayTime(kids.override_until) })}</span>{/if}
			</div>
			{#if kids?.paused_all_until}
				<p class="t-help" data-testid="kids-paused-all">{t('kids.device.pausedAll', { time: formatWeekdayTime(kids.paused_all_until) })}</p>
			{/if}
			{#if kids?.cooldowns?.length}
				<p class="t-help" data-testid="kids-pauses">{t('kids.device.pauses', { count: kids.cooldowns.length })}: {kids.cooldowns.map((c) => t('kids.device.pauseItem', { name: c.entity_id, time: formatWeekdayTime(c.until) })).join(', ')}</p>
			{/if}
			<span class="t-label" id="rel-l">{t('profiles.device.release')}</span>
			<div class="rel" role="group" aria-labelledby="rel-l" data-eq="release">
				{#each OVERRIDE_MINUTES as n (n)}
					<button type="button" class="btn btn-sm" onclick={() => release(n)} disabled={busy} aria-label={t('kids.device.releaseLabel', { n })} data-testid="kids-release-{n}">{t('kids.device.release', { n })}</button>
				{/each}
				<button type="button" class="btn btn-sm" onclick={() => release('bedtime_end')} disabled={busy} aria-label={t('kids.device.releaseMorningLabel')} data-testid="kids-release-morning">{t('kids.device.releaseMorning')}</button>
			</div>
			<div class="mt-2"><button type="button" class="btn btn-sm" onclick={lift} disabled={busy} data-testid="kids-lift"><Icon name="refresh" />{t('kids.device.lift')}</button></div>
			<p class="t-help mt-1">{t('kids.device.liftHint')}</p>
		</section>
	{/if}

	{#if !isGuest && !effective.profile.child_protection && ['ios', 'ipados', 'android', 'android_tablet'].includes(effective.platform || client.platform || 'ios')}
		<FamilyCard {client} {effective} onadjust={adjust} />
	{/if}

	<section class="card" aria-labelledby="pin-h" data-testid="device-pin">
		<div class="card-head"><Icon name="key" /><h2 id="pin-h" class="flex-1">{t('profiles.pin.deviceTitle')}</h2>
			<span class="badge" class:badge-ok={!!pin.source} data-testid="device-pin-state">
				{pin.source === 'profile' ? t('profiles.pin.fromProfile') : pin.source === 'device' ? t('profiles.pin.own') : t('profiles.pin.none')}
			</span>
		</div>
		<p class="t-help mb-3">{pin.fixed ? t('profiles.pin.fixedByProfile') : t('profiles.pin.deviceHint')}</p>
		<div class="btn2">
			<button type="button" class="btn btn-sm" onclick={() => (pinOpen = true)} disabled={busy || pin.fixed} data-testid="device-pin-set"><Icon name="key" /><span class="truncate">{pin.source === 'device' ? t('profiles.pin.change') : t('profiles.pin.setOwn')}</span></button>
			<button type="button" class="btn btn-sm" onclick={removePin} disabled={busy || pin.source !== 'device'} data-testid="device-pin-remove"><Icon name="trash" /><span class="truncate">{t('profiles.pin.removeOwn')}</span></button>
		</div>
	</section>

	<EffectiveRules {effective} {ctx} {busyRule} onadjust={adjust} onreset={resetRule} />

	<ProfileHistory clientId={client.id} {ctx} {profiles} reload={historyTick} />

	<div class="btn2 wide" data-eq="device-actions">
		<button type="button" class="btn" onclick={() => (resetOpen = true)} disabled={!client.override_count && !Object.values(effective.rules || {}).some((e) => e.source === 'device')} data-testid="reset-all"><Icon name="refresh" /><span class="truncate">{t('profiles.device.resetAll')}</span></button>
		<button type="button" class="btn" onclick={() => ((newMode = 'device'), (newOpen = true))} data-testid="profile-from-device"><Icon name="template" /><span class="truncate">{t('profiles.device.fromDevice')}</span></button>
	</div>
	{#if error}<Banner tone="error">{error}</Banner>{/if}

	<AssignProfileDialog bind:open={assignOpen} {client} {profiles} {ctx} onassigned={onAssigned} />
	<AssignProfileDialog bind:open={resetOpen} {client} {profiles} {ctx} reset onassigned={onAssigned} />
	<AdjustDialog bind:open={adjustOpen} {client} {effective} ruleKey={adjustKey} {ctx} onsaved={onAdjusted} />
	<NewProfileDialog bind:open={newOpen} bind:mode={newMode} {client} oncreated={onCreated} />
	<Modal bind:open={resetPreviewOpen} title={t('profiles.device.resetPreviewTitle')} description={t('profiles.editor.previewHint')} size="lg" testid="reset-preview-dialog" dismissible={!busyRule}>
		{#if resetPreview}
			<div class="stack">
				<PreviewView preview={resetPreview} {ctx} testid="reset-preview" />
				<LooseningWarning rules={resetPreview.loosened_rules || []} bind:confirmed={resetConfirmed} subject={client.name} />
			</div>
		{/if}
		{#snippet footer({ close })}
			<div class="foot-eq">
				<button type="button" class="btn" onclick={close} disabled={!!busyRule}>{t('common.back')}</button>
				<button type="button" class="btn btn-primary" onclick={confirmReset} disabled={!!busyRule || (!!resetPreview?.loosened_rules?.length && !resetConfirmed)} data-testid="reset-preview-confirm">{t('profiles.device.useProfile')}</button>
			</div>
		{/snippet}
	</Modal>
	<RemotePinDialog bind:open={pinOpen} title={t('profiles.pin.deviceDialog', { name: client.name })} description={t('profiles.pin.deviceDialogHint')} onsave={savePin} testid="device-pin-dialog" />
{/if}

<style>
	.pgrid {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 14px;
	}
	.pcol {
		display: grid;
		gap: 6px;
		align-content: start;
		justify-items: start;
		min-width: 0;
	}
	.pcol .select {
		width: 100%;
	}
	.chip-row {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
		min-width: 0;
	}
	.pchip {
		display: inline-flex;
		gap: 8px;
		align-items: center;
		font-weight: 650;
		color: var(--ink);
		min-width: 0;
		max-width: 100%;
	}
	.early {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 10px;
	}
	.areq {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 10px;
	}
	.areq .note {
		overflow-wrap: anywhere;
	}
	/* Equal sizes: equal buttons. */
	.rel {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(8.5rem, 1fr));
		gap: 8px;
		margin-top: 4px;
	}
	.rel .btn {
		min-width: 0;
		justify-content: center;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.btn2 {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
		max-width: 420px;
	}
	.btn2.wide {
		max-width: 560px;
	}
	.btn2 .btn {
		justify-content: center;
		min-width: 0;
	}
	@media (max-width: 760px) {
		.pgrid {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	@media (max-width: 420px) {
		.btn2,
		.btn2.wide {
			grid-template-columns: minmax(0, 1fr);
			max-width: none;
		}
	}
</style>
