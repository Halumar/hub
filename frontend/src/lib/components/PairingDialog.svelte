<script>
	/**
	 * Pairing dialog: access member | guest, owner, profile (tiles;
	 * members: default of the device type unless one is picked, guests: the
	 * default guest profile, never one with parental controls), own area,
	 * guests additionally the duration (1/3/7/14/30 days or a date, at most
	 * 90 days per grant). Right column: QR code (server-built deep link),
	 * large PIN, countdown, TLS address and fingerprint. Mounted once in the
	 * layout, opened via openPairing(). Closing the dialog cancels an active PIN.
	 */
	import QRCode from 'qrcode';
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { pairingDialog } from '$lib/stores/pairing.svelte.js';
	import { deviceUserName } from '$lib/device-user.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { copyText, formatDuration, formatWeekdayTime, toLocalInput } from '$lib/utils.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import BrandMark from '$lib/ui/BrandMark.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import ProfilePicker from '$lib/components/profiles/ProfilePicker.svelte';
	import RemoteKeyWarning from '$lib/components/profiles/RemoteKeyWarning.svelte';
	import { pairingHubRemote, PAIRABLE_GUEST_PLATFORMS } from '$lib/profiles/preview.js';
	import { loadCatalog, guestAccess, controlPathOf } from '$lib/profiles/catalog.svelte.js';
	import { GUEST_DAYS } from '$lib/profiles/format.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { DEVICE_PLATFORMS, SHOWN_DEVICE_PLATFORMS, defaultProfilesSummary } from '$lib/device-info.js';

	const ACCESS = [
		{ id: 'member', icon: 'user' },
		{ id: 'guest', icon: 'users' },
	];

	// Form
	let owner = $state('');
	let profileId = $state(null);
	let ownAreaId = $state('');
	let guestHours = $state(24);
	let expiryMode = $state('duration'); // 'duration' | 'date'
	let expiryDate = $state('');

	// Reference data
	let profiles = $state([]);
	let settings = $state(null);
	let areas = $state([]);
	let owners = $state([]);
	let loadError = $state('');

	// PIN state
	let pin = $state('');
	let remaining = $state(0);
	let total = $state(300);
	let deepLink = $state('');
	let serverUrl = $state('');
	let qr = $state('');
	let generating = $state(false);
	let dirty = $state(false);
	let timer = null;

	let role = $derived(pairingDialog.role === 'guest' ? 'guest' : 'member');
	let isGuest = $derived(role === 'guest');
	let active = $derived(!!pin && remaining > 0);
	let maxHours = $derived(guestAccess().max_hours || 2160);

	let previewName = $derived(deviceUserName({ access: role, owner_label: owner, name: t('pairing.deviceNamePlaceholder') }));

	let expiryAt = $derived.by(() => {
		if (expiryMode === 'date' && expiryDate) return new Date(expiryDate);
		return new Date(Date.now() + guestHours * 3600_000);
	});
	let dateValid = $derived(expiryMode !== 'date' || (!!expiryDate && expiryAt.getTime() > Date.now() && expiryAt.getTime() <= Date.now() + maxHours * 3600_000));

	let defaults = $derived(settings?.default_profiles || {});
	let byId = $derived(Object.fromEntries(profiles.map((p) => [p.id, p])));
	let autoHint = $derived(
		defaultProfilesSummary(
			defaults,
			Object.fromEntries(profiles.map((p) => [p.id, p.name])),
			SHOWN_DEVICE_PLATFORMS.map((p) => p.id),
		),
	);
	let chosen = $derived(profileId ? byId[profileId] : null);
	let remoteText = $derived.by(() => {
		if (!chosen) return t('pairing.remoteByType');
		return chosen.rules?.remote_access ? t('pairing.remoteOnByProfile', { name: chosen.name }) : t('pairing.remoteOffByProfile', { name: chosen.name });
	});

	/** "Hub only" with remote access: the device gets a read-only key. Without a picked profile any default profile of a phone or tablet counts. */
	// A guest device without a stored level is on "Hub only", whatever the profile kind.
	// The hub's answer per access and platform (hub_remote_pairing); without it the profile's own values.
	const PAIR_PLATFORMS = DEVICE_PLATFORMS.filter((p) => p.cls === 'phone' || p.cls === 'tablet').map((p) => p.id);
	function isRemoteKey(p, platforms = PAIR_PLATFORMS) {
		if (!p) return false;
		const hub = pairingHubRemote(p, isGuest ? 'guest' : 'member', isGuest ? PAIRABLE_GUEST_PLATFORMS : platforms);
		if (hub !== null) return hub;
		return p.rules?.remote_access === true && controlPathOf(p, { guest: isGuest }) === 'hub';
	}
	let remoteKey = $derived.by(() => {
		if (chosen) return isRemoteKey(chosen);
		if (isGuest) return false;
		return PAIR_PLATFORMS.some((id) => isRemoteKey(byId[defaults[id]], [id]));
	});

	let linkParams = $derived(new URLSearchParams(deepLink.split('?')[1] || ''));
	// Under Ingress the server reports the plain-HTTP base (:8099); with TLS
	// (deep link carries tls_port + fp) show the encrypted endpoint.
	let manualUrl = $derived.by(() => {
		if (!serverUrl) return '';
		const tlsPort = linkParams.get('tls_port');
		try {
			const u = new URL(serverUrl);
			if (tlsPort && u.protocol === 'http:' && u.port === '8099') return `https://${u.hostname}:${tlsPort}`;
		} catch {
			/* show as-is */
		}
		return serverUrl;
	});
	let fingerprint = $derived(linkParams.get('fp') || '');
	let fingerprintShort = $derived(fingerprint.length > 16 ? `${fingerprint.slice(0, 8)}…${fingerprint.slice(-5)}` : fingerprint);

	// Ring: fraction of the PIN lifetime left
	const R = 9;
	const C = 2 * Math.PI * R;
	let dash = $derived(`${(C * Math.max(0, Math.min(1, remaining / (total || 300)))).toFixed(1)} ${C.toFixed(1)}`);

	$effect(() => {
		if (pairingDialog.open) untrack(onOpen);
	});

	$effect(() => {
		// Any change of the form while a PIN runs applies to the next PIN only.
		void [role, owner, profileId, ownAreaId, guestHours, expiryMode, expiryDate];
		if (untrack(() => active)) dirty = true;
	});

	$effect(() => {
		const off = onBroadcast('client_paired', async (data) => {
			// Only the device that used this dialog's PIN closes it. Guests invited by a
			// member and devices set up from a paired device pair while this PIN stays active.
			if (!pin || remaining <= 0) return;
			const mine = pin;
			try {
				// A pairing with this PIN uses it up; while a PIN is still active, the device was another one.
				if ((await api.getPairingStatus())?.pairing_active) return;
			} catch {
				/* status unknown: close as before */
			}
			if (pin !== mine) return;
			resetPin();
			pairingDialog.open = false;
			if (data?.client_name) addToast(t('pairing.paired', { name: data.client_name }), 'success');
			if (data?.client_id && !pairingDialog.stay) goto(`${base}/clients/${data.client_id}`);
		});
		return off;
	});

	async function onOpen() {
		loadError = '';
		dirty = false;
		const [st, pr, se, cl, ar] = await Promise.allSettled([
			api.getPairingStatus(),
			api.listProfiles(),
			api.getProfileSettings(),
			api.getPairedClients(),
			api.getAreas(),
			loadCatalog(),
		]);
		if (pr.status === 'fulfilled') profiles = pr.value?.profiles || [];
		settings = se.status === 'fulfilled' ? se.value : null;
		areas = ar.status === 'fulfilled' && Array.isArray(ar.value) ? ar.value : [];
		if (cl.status === 'fulfilled') {
			owners = [...new Set((cl.value || []).map((c) => (c.owner_label || '').trim()).filter(Boolean))].sort();
		}
		applyRoleDefaults();
		if (st.status === 'rejected') {
			loadError = t('pairing.failedLoad', { message: st.reason?.message || '' });
			return;
		}
		const s = st.value || {};
		if (s.pairing_active && s.remaining_seconds > 0 && s.pin) {
			if (s.is_guest_pin) pairingDialog.role = 'guest';
			await showPin(s.pin, s.remaining_seconds, s.deep_link || '', s.url || '', Math.max(300, s.remaining_seconds));
			dirty = false;
		}
	}

	/** Members: default of the device type (null); guests: default guest profile and duration. */
	function applyRoleDefaults() {
		if (isGuest) {
			const g = settings?.default_profiles?.guest;
			profileId = g && byId[g] && !byId[g].child_protection ? g : profiles.find((p) => !p.child_protection)?.id || null;
			guestHours = Math.min(settings?.guest_default_hours || guestAccess().default_hours || 24, maxHours);
			expiryMode = 'duration';
			if (!expiryDate) expiryDate = toLocalInput(new Date(Date.now() + guestHours * 3600_000));
		} else {
			profileId = null;
		}
	}

	async function showPin(p, secs, link, url, lifetime = 300) {
		pin = p;
		remaining = secs;
		total = lifetime;
		deepLink = link;
		serverUrl = url;
		startTimer();
		try {
			qr = link
				? await QRCode.toDataURL(link, { width: 240, margin: 1, color: { dark: '#121317', light: '#ffffff' }, errorCorrectionLevel: 'M' })
				: '';
		} catch {
			qr = '';
		}
	}

	function startTimer() {
		clearInterval(timer);
		timer = setInterval(() => {
			remaining = Math.max(0, remaining - 1);
			if (remaining <= 0) clearInterval(timer);
		}, 1000);
	}

	function resetPin() {
		clearInterval(timer);
		pin = '';
		remaining = 0;
		deepLink = '';
		serverUrl = '';
		qr = '';
		dirty = false;
	}

	function setRole(id) {
		pairingDialog.role = id;
		applyRoleDefaults();
	}

	function pickDays(d) {
		expiryMode = 'duration';
		guestHours = d * 24;
	}

	function pickDate() {
		expiryMode = 'date';
		if (!expiryDate) expiryDate = toLocalInput(new Date(Date.now() + guestHours * 3600_000));
	}

	function guestReason(p) {
		return isGuest && p.child_protection ? t('profiles.err.guestChildShort') : '';
	}

	async function generate() {
		if (isGuest && (!profileId || !dateValid)) return;
		generating = true;
		try {
			const body = { access: role };
			if (profileId) body.profile_id = profileId;
			if (ownAreaId) body.own_area_id = ownAreaId;
			if (owner.trim()) body.owner_label = owner.trim();
			if (isGuest) {
				if (expiryMode === 'date' && expiryDate) body.guest_expires_at = new Date(expiryDate).toISOString();
				else body.guest_hours = guestHours;
			}
			const res = await api.generatePairingPin(body);
			await showPin(res.pin, res.expires_in, res.deep_link || '', res.url || '', res.expires_in || 300);
			dirty = false;
			addToast(t(isGuest ? 'pairing.guestPinGenerated' : 'pairing.pinGenerated'), 'success');
		} catch (e) {
			addToast(t('pairing.failedGenerate', { message: errorText(e) }), 'error');
		} finally {
			generating = false;
		}
	}

	async function cancelPin({ toast = true } = {}) {
		if (!pin) return;
		try {
			await api.cancelPairing();
			if (toast) addToast(t('pairing.cancelled'), 'success');
		} catch (e) {
			addToast(t('pairing.failedCancel', { message: e.message }), 'error');
		} finally {
			resetPin();
		}
	}

	function onClose() {
		if (active) cancelPin();
		else resetPin();
	}

	async function copyLink() {
		if (!deepLink) return;
		const ok = await copyText(deepLink);
		addToast(ok ? t('pairing.linkCopied') : t('common.copyFailed'), ok ? 'success' : 'error');
	}
</script>

<Modal bind:open={pairingDialog.open} title={isGuest ? t('pairing.inviteGuest') : t('pairing.title')} description={t('pairing.description')} size="xl" onclose={onClose} bodyClass="p-0" testid="pairing-dialog">
	<div class="pair">
		<div class="pair-form stack">
			{#if loadError}<Banner tone="error">{loadError}</Banner>{/if}

			<fieldset class="field">
				<legend class="field-label mb-1.5">{t('pairing.access')}</legend>
				<div class="opts two" role="group" aria-label={t('pairing.access')} data-eq="access">
					{#each ACCESS as r (r.id)}
						<button type="button" class="opt" aria-pressed={role === r.id ? 'true' : 'false'} onclick={() => setRole(r.id)} data-testid="pairing-role-{r.id}">
							<Icon name={r.icon} />{t(`pairing.roles.${r.id}`)}
						</button>
					{/each}
				</div>
				<p class="t-help">{t(`pairing.roleHint.${role}`)}</p>
			</fieldset>

			<Field label={isGuest ? t('pairing.guestName') : t('pairing.owner')} hint={t('pairing.userNameHint', { name: previewName })} id="pairing-owner">
				{#snippet children({ id, describedby })}
					<input {id} class="input" maxlength="50" list="pairing-owners" placeholder={isGuest ? t('pairing.guestNamePlaceholder') : t('pairing.ownerPlaceholder')} aria-describedby={describedby} bind:value={owner} autocomplete="off" />
					<datalist id="pairing-owners">
						{#each owners as o (o)}<option value={o}></option>{/each}
					</datalist>
				{/snippet}
			</Field>

			<fieldset class="field">
				<legend class="field-label mb-1.5">{t('pairing.profile')}</legend>
				<ProfilePicker
					{profiles}
					bind:value={profileId}
					label={t('pairing.profile')}
					auto={isGuest ? null : { label: t('pairing.profileAuto'), hint: autoHint || t('pairing.profileAutoHint') }}
					disabledReason={guestReason}
					name="pairing-profile"
					testid="pairing-profile"
				/>
				<p class="t-help remote" data-testid="pairing-remote"><Icon name="globe" size="14" />{remoteText}</p>
				{#if remoteKey}<RemoteKeyWarning class="mt-2" testid="pairing-remote-key-warning" />{/if}
			</fieldset>

			<label class="field">
				<span class="field-label">{t('profiles.device.ownArea')}</span>
				<select class="select" bind:value={ownAreaId} data-testid="pairing-own-area">
					<option value="">{t('profiles.device.noOwnArea')}</option>
					{#each areas as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
				</select>
				<span class="t-help">{t('profiles.device.ownAreaHint')}</span>
			</label>

			{#if isGuest}
				<fieldset class="field">
					<legend class="field-label mb-1.5">{t('pairing.accessUntil')}</legend>
					<div class="durs" role="group" aria-label={t('pairing.accessUntil')} data-eq="guest-duration">
						{#each GUEST_DAYS as d (d)}
							<button type="button" class="chip dur" aria-pressed={expiryMode === 'duration' && guestHours === d * 24 ? 'true' : 'false'} onclick={() => pickDays(d)} data-testid="pairing-days-{d}">{t('profiles.days', { count: d })}</button>
						{/each}
						<button type="button" class="chip dur" aria-pressed={expiryMode === 'date' ? 'true' : 'false'} onclick={pickDate} data-testid="pairing-expiry-date">{t('pairing.pickDate')}</button>
					</div>
					{#if expiryMode === 'date'}
						<label class="sr-only" for="pairing-expiry">{t('pairing.expiryDate')}</label>
						<input id="pairing-expiry" type="datetime-local" class="input mt-2" bind:value={expiryDate} />
						{#if !dateValid}<p class="field-error" role="alert">{t('pairing.expiryTooLong', { days: Math.round(maxHours / 24) })}</p>{/if}
					{/if}
					<p class="t-help" data-testid="pairing-expiry-text">
						{t('pairing.expiryDateText', { date: formatWeekdayTime(expiryAt) })} · {t('pairing.maxDays', { days: Math.round(maxHours / 24) })}
					</p>
				</fieldset>
			{/if}
		</div>

		<div class="pair-code">
			{#if active}
				{#if qr}<img class="qr" src={qr} alt={t('pairing.qrAlt')} />{/if}
				<p class="pin" aria-label="{t('pairing.pin')} {pin}" data-testid="pairing-pin">{pin}</p>
				<p class="pin-ring" role="timer" aria-live="off">
					<svg viewBox="0 0 24 24" aria-hidden="true">
						<circle cx="12" cy="12" r={R} fill="none" stroke="var(--fill-3)" stroke-width="3" />
						<circle cx="12" cy="12" r={R} fill="none" stroke={remaining < 60 ? 'var(--warn)' : 'var(--ok)'} stroke-width="3" stroke-dasharray={dash} transform="rotate(-90 12 12)" stroke-linecap="round" />
					</svg>
					{t('pairing.validFor', { time: formatDuration(remaining) })}
				</p>
				<p class="t-help">{t('pairing.scanHint')}</p>
				{#if manualUrl}
					<dl class="dl box w-full text-left">
						<dt>{t('pairing.address')}</dt>
						<dd class="t-mono select-all" data-testid="pairing-url">{manualUrl}</dd>
						{#if fingerprintShort && manualUrl.startsWith('https://')}
							<dt>{t('pairing.fingerprint')}</dt>
							<dd class="t-mono" title={t('pairing.fingerprintTitle')} data-testid="pairing-fingerprint">{fingerprintShort}</dd>
						{/if}
					</dl>
				{/if}
				{#if deepLink}
					<button type="button" class="btn btn-sm" onclick={copyLink}><Icon name="link" />{t('pairing.copyLink')}</button>
				{/if}
			{:else}
				<div class="state">
					<Icon name="qr" />
					<h3>{pin ? t('pairing.expiredPin') : t('pairing.generate')}</h3>
					<p>{t('pairing.notActive')}</p>
				</div>
			{/if}
		</div>
	</div>

	{#snippet footer({ close })}
		<span class="lead t-help" aria-live="polite">
			{#if active && !dirty}<BrandMark size={18} state="loading" />{/if}
			{#if active}{dirty ? t('pairing.changesNeedNewPin') : t('pairing.waiting')}{/if}
		</span>
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close}>{t('pairing.cancelPin')}</button>
			<button type="button" class="btn btn-primary" onclick={generate} disabled={generating || (isGuest && (!profileId || !dateValid))} data-testid="pairing-generate">
				<Icon name={active ? 'refresh' : 'qr'} />
				{generating ? t('pairing.generating') : active ? t('pairing.newPin') : t('pairing.generate')}
			</button>
		</div>
	{/snippet}
</Modal>

<style>
	.lead {
		display: inline-flex;
		align-items: center;
		gap: var(--s2);
	}
	.pair {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
	}
	.pair-form {
		padding: 16px 24px 20px;
		border-right: 1px solid var(--line);
		align-content: start;
	}
	.pair-code {
		padding: 20px 24px;
		background: var(--surface-2);
		display: grid;
		justify-items: center;
		align-content: start;
		gap: 10px;
		text-align: center;
	}
	.qr {
		width: 220px;
		height: 220px;
		background: #fff;
		border-radius: 16px;
		padding: 12px;
		box-shadow: 0 1px 3px rgb(0 0 0 / 0.08);
	}
	.pin {
		font-family: var(--mono);
		font-size: 34px;
		font-weight: 750;
		letter-spacing: 0.06em;
		margin: 0;
		font-variant-numeric: tabular-nums;
	}
	.pin-ring {
		display: flex;
		align-items: center;
		gap: 8px;
		font-weight: 650;
		color: var(--ink-2);
		margin: 0;
	}
	.pin-ring svg {
		width: 22px;
		height: 22px;
	}
	.opts.two {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	/* Equal sizes: six equal duration buttons, 3 × 2. */
	.durs {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 6px;
	}
	.dur {
		justify-content: center;
		min-width: 0;
		min-height: var(--h-ctl);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.remote {
		display: flex;
		gap: 6px;
		align-items: center;
		margin-top: 6px;
	}
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
		min-width: 0;
	}
	@media (max-width: 760px) {
		.pair {
			grid-template-columns: minmax(0, 1fr);
		}
		.pair-code {
			order: -1;
		}
		.pair-form {
			border-right: 0;
			padding: 16px;
		}
		.qr {
			width: 180px;
			height: 180px;
		}
	}
</style>
