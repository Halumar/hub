<script>
	/**
	 * House setting "Default profiles": one profile per device type plus
	 * the default guest duration. Applies wherever nobody picks a profile
	 * (pairing preselection, PIN without profile, adding by a phone, member
	 * invitations, pairing through the integration).
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import LoadError from '$lib/components/LoadError.svelte';
	import { GUEST_DAYS } from '$lib/profiles/format.js';
	import { DEVICE_PLATFORMS, NO_APP_PLATFORMS, SHOWN_DEVICE_PLATFORMS, platformIcon } from '$lib/device-info.js';
	import { controlPathOf } from '$lib/profiles/catalog.svelte.js';
	import RemoteKeyWarning from './RemoteKeyWarning.svelte';
	import { pairingHubRemote, PAIRABLE_GUEST_PLATFORMS } from '$lib/profiles/preview.js';

	let { profiles = [], onsaved = null, compact = false } = $props();

	// Apple and Android device types with an app, then guests. Platforms without
	// an app (Wear OS, web) are not shown; their stored values are saved unchanged.
	const SLOTS = [
		...SHOWN_DEVICE_PLATFORMS.map((p) => ({ id: p.id, icon: platformIcon(p.id) })),
		{ id: 'guest', icon: 'users' },
	];

	let saved = $state(null);
	let draft = $state(null);
	let error = $state('');
	let saving = $state(false);

	onMount(load);

	async function load() {
		try {
			saved = await api.getProfileSettings();
			draft = JSON.parse(JSON.stringify(saved));
			error = '';
		} catch (e) {
			error = e.message;
		}
	}

	let dirty = $derived(!!saved && JSON.stringify(saved) !== JSON.stringify(draft));

	// Slots whose default hands out the read-only key: phones, tablets and guests on a profile that is
	// "Hub only" with remote access (a guest device without a stored level is on "Hub only"). TVs and
	// watches never run on "Hub only".
	const KEY_SLOTS = new Set(DEVICE_PLATFORMS.filter((p) => p.cls === 'phone' || p.cls === 'tablet').map((p) => p.id));
	let byId = $derived(Object.fromEntries(profiles.map((p) => [p.id, p])));
	function remoteKeySlot(slot) {
		const p = byId[draft?.default_profiles?.[slot]];
		if (!p) return false;
		// The hub's answer per access and platform; without it the profile's own values.
		const hub = slot === 'guest' ? pairingHubRemote(p, 'guest', PAIRABLE_GUEST_PLATFORMS) : pairingHubRemote(p, 'member', [slot]);
		if (hub !== null) return hub;
		if (p.rules?.remote_access !== true) return false;
		if (slot === 'guest') return controlPathOf(p, { guest: true }) === 'hub';
		return KEY_SLOTS.has(slot) && controlPathOf(p) === 'hub';
	}
	let remoteKeySlots = $derived(draft ? SLOTS.filter((s) => remoteKeySlot(s.id)).map((s) => s.id) : []);

	// A platform without an app keeps its stored default, but follows the shown
	// platform of the same device class (Wear OS the Apple Watch) when that one
	// is changed away from the profile both pointed to, so no hidden slot keeps a
	// profile in use that the admin cannot see.
	function withHiddenSlots(body) {
		const before = saved?.default_profiles || {};
		const after = { ...(body.default_profiles || {}) };
		for (const hidden of DEVICE_PLATFORMS.filter((p) => NO_APP_PLATFORMS.has(p.id))) {
			const sibling = SHOWN_DEVICE_PLATFORMS.find((p) => p.cls === hidden.cls);
			if (!sibling) continue;
			if (before[hidden.id] === before[sibling.id] && after[sibling.id] !== before[sibling.id]) {
				after[hidden.id] = after[sibling.id];
			}
		}
		return { ...body, default_profiles: after };
	}

	async function save() {
		saving = true;
		try {
			saved = await api.saveProfileSettings(withHiddenSlots(draft));
			draft = JSON.parse(JSON.stringify(saved));
			addToast(t('profiles.defaults.saved'), 'success');
			onsaved?.(saved);
		} catch (e) {
			addToast(e.code === 'profile.guest_child_protection' ? t('profiles.err.guestChild') : t('profiles.defaults.failed', { message: e.message }), 'error');
		} finally {
			saving = false;
		}
	}

	function hoursLabel(h) {
		return h % 24 === 0 ? t('profiles.days', { count: h / 24 }) : t('profiles.hours', { count: h });
	}
</script>

<section class="card" aria-labelledby="defprof-h" data-testid="default-profiles">
	<div class="card-head"><Icon name="check" /><h2 id="defprof-h" class="flex-1">{t('profiles.defaults.title')}</h2></div>
	<p class="t-help mb-3">{t('profiles.defaults.hint')}</p>
	{#if error}
		<LoadError message={error} onretry={load} />
	{:else if draft}
		<div class="slots" class:compact>
			{#each SLOTS as s (s.id)}
				<label class="field slot">
					<span class="field-label inline-flex items-center gap-1"><Icon name={s.icon} size="14" />{t(`profiles.slots.${s.id}`)}</span>
					<select class="select" bind:value={draft.default_profiles[s.id]} data-testid="default-{s.id}">
						{#each profiles as p (p.id)}
							<option value={p.id} disabled={s.id === 'guest' && p.child_protection}>{p.name}{s.id === 'guest' && p.child_protection ? ` (${t('profiles.childProtection')})` : ''}</option>
						{/each}
					</select>
				</label>
			{/each}
		</div>
		{#if remoteKeySlots.length}
			<p class="t-help mt-3" data-testid="default-remote-key-slots">{t('profiles.defaults.remoteKeySlots', { slots: remoteKeySlots.map((s) => t(`profiles.slots.${s}`)).join(', ') })}</p>
			<RemoteKeyWarning class="mt-1" testid="default-remote-key-warning" />
		{/if}
		<div class="field mt-3">
			<span class="field-label" id="gdh-l">{t('profiles.defaults.guestDuration')}</span>
			<div class="durs" role="group" aria-labelledby="gdh-l" data-eq="guest-hours">
				{#each GUEST_DAYS as d (d)}
					<button type="button" class="chip dur" aria-pressed={draft.guest_default_hours === d * 24 ? 'true' : 'false'} onclick={() => (draft.guest_default_hours = d * 24)} data-testid="guest-hours-{d * 24}">{hoursLabel(d * 24)}</button>
				{/each}
			</div>
			{#if !GUEST_DAYS.includes(draft.guest_default_hours / 24)}<p class="t-help">{t('profiles.defaults.currentDuration', { value: hoursLabel(draft.guest_default_hours) })}</p>{/if}
			<p class="t-help">{t('profiles.defaults.guestDurationHint')}</p>
		</div>
		{#if dirty}
			<div class="row mt-3">
				<button type="button" class="btn btn-primary" onclick={save} disabled={saving} data-testid="default-profiles-save">{saving ? t('common.saving') : t('common.save')}</button>
				<button type="button" class="btn btn-ghost" onclick={() => (draft = JSON.parse(JSON.stringify(saved)))} disabled={saving}>{t('common.cancel')}</button>
			</div>
		{/if}
	{/if}
</section>

<style>
	.slots {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
		gap: 10px 14px;
	}
	.slot .select {
		width: 100%;
		min-width: 0;
	}
	/* Equal sizes: equal duration buttons. */
	.durs {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 6px;
	}
	.dur {
		justify-content: center;
		min-width: 0;
		min-height: var(--h-ctl);
		padding-inline: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	@media (max-width: 480px) {
		.durs {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
	}
</style>
