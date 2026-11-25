<script>
	/**
	 * "Adjust" one rule on one device: new value, how long (1 hour ·
	 * today · until tomorrow morning · 7 days · date · permanent, six equal
	 * tiles), confirmation when a security rule gets looser, optional note.
	 * The hub computes the end of a preset in its time zone; the text here
	 * is the local estimate.
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import RuleValueEditor from './RuleValueEditor.svelte';
	import LooseningWarning from './LooseningWarning.svelte';
	import RemoteKeyWarning from './RemoteKeyWarning.svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L, KEY_PLATFORMS, expiryPresets, limits, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { clone, formatValue, loosens, presetEnd, same } from '$lib/profiles/format.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { formatWeekdayTime, toLocalInput } from '$lib/utils.js';

	let { open = $bindable(false), client, effective, ruleKey, ctx = {}, onsaved = null } = $props();

	let value = $state(null);
	let userChoice = $state(false);
	let duration = $state('permanent');
	let date = $state('');
	let note = $state('');
	let confirmed = $state(false);
	let busy = $state(false);
	let error = $state('');

	let rule = $derived(ruleOf(ruleKey));
	let entry = $derived(effective?.rules?.[ruleKey]);
	let profileValue = $derived(entry?.profile_value !== undefined ? entry.profile_value : entry?.value);
	let existing = $derived((effective?.overrides || []).find((o) => o.rule === ruleKey) || null);
	let profileName = $derived(effective?.profile?.name || client?.profile_name || '');

	$effect(() => {
		if (open && entry) {
			value = clone(entry.value);
			userChoice = !!entry.user_choice;
			duration = existing?.expires_at ? 'date' : 'permanent';
			date = toLocalInput(existing?.expires_at ? new Date(existing.expires_at) : new Date(Date.now() + 2 * 3600_000));
			note = existing?.note || '';
			confirmed = false;
			error = '';
		}
	});

	const DURATIONS = $derived([...expiryPresets().map((p) => ({ id: p.id, label: L(p.label) })), { id: 'date', label: t('profiles.adjust.date') }, { id: 'permanent', label: t('profiles.adjust.permanent') }]);

	let end = $derived(duration === 'date' ? (date ? new Date(date) : null) : duration === 'permanent' ? null : presetEnd(duration));
	let dateValid = $derived(duration !== 'date' || (!!end && end.getTime() > Date.now() + 5 * 60_000 && end.getTime() < Date.now() + 365 * 86400_000));
	let isLoosening = $derived(loosens(rule, entry?.value, value));
	let equalsProfile = $derived(same(value, profileValue) && !userChoice);
	let unchanged = $derived(same(value, entry?.value) && userChoice === !!entry?.user_choice && (duration === 'permanent') === !existing?.expires_at && !(duration === 'date' && existing?.expires_at));
	let noteMax = $derived(limits().note_max_length || 200);
	// A TV or watch always holds its own key: "Hub only" is not offered there.
	let keyPlatform = $derived(KEY_PLATFORMS.includes(client?.platform || effective?.platform));
	let editCtx = $derived(keyPlatform ? { ...ctx, hiddenChoices: { control_path: ['hub'] } } : ctx);
	// Remote access on while the device stays on "Hub only" (also as a temporary adjustment).
	let pathAfter = $derived(ruleKey === 'control_path' ? value : effective?.rules?.control_path?.value);
	let remoteAfter = $derived(ruleKey === 'remote_access' ? value : effective?.rules?.remote_access?.value);
	let remoteKey = $derived(pathAfter === 'hub' && remoteAfter === true && (ruleKey === 'remote_access' || ruleKey === 'control_path'));
	// A temporary adjustment that hands out a key the profile value does not: at its end the key,
	// widgets, Siri and the watch go together.
	const holds = (path, remote) => path === 'checked' || path === 'direct' || remote === true;
	// The state when this adjustment ends: the adjusted rule falls back to the profile value, the other
	// rule keeps its value unless its own temporary value ends before.
	const valueAt = (key, t) => {
		const e = effective?.rules?.[key];
		return e?.expires_at && t && Date.parse(e.expires_at) <= t.getTime() ? e.then : e?.value;
	};
	let pathThen = $derived.by(() => {
		const p = ruleKey === 'control_path' ? profileValue : valueAt('control_path', end);
		return keyPlatform && p === 'hub' ? 'checked' : p;
	});
	let remoteThen = $derived(ruleKey === 'remote_access' ? profileValue : valueAt('remote_access', end));
	let accessRule = $derived(ruleKey === 'remote_access' || ruleKey === 'control_path');
	let keyEnds = $derived(!!end && accessRule && holds(pathAfter, remoteAfter) && !holds(pathThen, remoteThen));
	// Fallback for a hub that does not answer with hub_remote_now / hub_remote_at: this adjustment's own
	// end, and any scheduled end of an access rule counts as unsure (the warning shows).
	let remoteKeyAtEnd = $derived(!!end && accessRule && !remoteKey && pathThen === 'hub' && remoteThen === true);
	let otherEnds = $derived(['control_path', 'remote_access'].some((k) => k !== ruleKey && (effective?.rules?.[k]?.expires_at || (effective?.overrides || []).some((o) => o.rule === k && o.expires_at))));
	let fallbackNow = $derived(remoteKey || (accessRule && otherEnds && (pathAfter === 'hub' || remoteAfter === true)));

	// The hub decides: a dry run of exactly this adjustment answers hub_remote_now / hub_remote_at for the device.
	let hubState = $state(null);
	let dryPending = $state(false);
	let dryRunSeq = 0;
	function adjustBody() {
		const body = { value: clone(value) };
		if (rule?.user_choice_allowed) body.user_choice = userChoice;
		if (duration === 'date') body.expires_at = end?.toISOString();
		else if (duration !== 'permanent') body.expires = duration;
		return body;
	}
	$effect(() => {
		if (!open || !accessRule || !entry || !dateValid) {
			// Closed or nothing to ask: an answer still on its way is dropped.
			dryRunSeq += 1;
			hubState = null;
			dryPending = false;
			return;
		}
		const body = adjustBody();
		const seq = ++dryRunSeq;
		dryPending = true;
		const timer = setTimeout(async () => {
			let next = null;
			try {
				const pv = await api.setClientOverride(client.id, ruleKey, { ...body, note: null }, { dryRun: true });
				const d = (pv?.devices || []).find((x) => x.client_id === client.id);
				// Not listed: the change leaves its pair as it is, which the device's own effective rules carry.
				if (!d) next = Object.prototype.hasOwnProperty.call(effective || {}, 'hub_remote_now') ? { now: !!effective.hub_remote_now, at: effective.hub_remote_at || null } : null;
				else if (Object.prototype.hasOwnProperty.call(d, 'hub_remote_now')) next = { now: !!d.hub_remote_now, at: d.hub_remote_at || null };
			} catch {
				// No answer (error or timeout): warn rather than block.
				next = { now: true, at: null };
			}
			if (seq !== dryRunSeq) return;
			hubState = next;
			dryPending = false;
		}, 250);
		return () => clearTimeout(timer);
	});
	let warnNow = $derived(hubState ? hubState.now : fallbackNow);
	let warnAt = $derived(hubState ? (!hubState.now && hubState.at ? hubState.at : null) : remoteKeyAtEnd && !fallbackNow ? end : null);

	async function save() {
		busy = true;
		error = '';
		const body = { value, note: note.trim() || null };
		if (rule?.user_choice_allowed) body.user_choice = userChoice;
		if (duration === 'date') body.expires_at = end.toISOString();
		else if (duration !== 'permanent') body.expires = duration;
		try {
			const res = await api.setClientOverride(client.id, ruleKey, body);
			addToast(equalsProfile ? t('profiles.adjust.removedSame') : t('profiles.adjust.saved', { rule: L(rule?.label) }), 'success');
			open = false;
			onsaved?.(res);
		} catch (e) {
			error = errorText(e);
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:open title={t('profiles.adjust.title', { rule: L(rule?.label) })} description={t('profiles.adjust.hint', { device: client?.name || '', profile: profileName })} size="lg" testid="adjust-dialog" dismissible={!busy}>
	{#if rule && entry}
		<div class="stack">
			<p class="box"><span class="t-label">{t('profiles.adjust.inProfile', { profile: profileName })}</span> <b>{formatValue(ruleKey, profileValue, ctx)}</b></p>
			<div class="field">
				<span class="field-label">{t('profiles.adjust.newValue')}</span>
				<RuleValueEditor {rule} bind:value ctx={editCtx} idPrefix="adj" />
				{#if warnNow}<RemoteKeyWarning testid="adjust-remote-key-warning" />{/if}
				{#if warnAt}<RemoteKeyWarning from={warnAt} testid="adjust-remote-key-warning-end" />{/if}
				{#if rule.user_choice_allowed}
					<label class="check">
						<input type="checkbox" bind:checked={userChoice} data-testid="adjust-user-choice" />
						{t('profiles.userChoiceLong')}
					</label>
				{/if}
				{#if equalsProfile}<p class="t-help" data-testid="adjust-same">{t('profiles.adjust.sameAsProfile')}</p>{/if}
			</div>
			<fieldset class="field">
				<legend class="field-label mb-1.5">{t('profiles.adjust.howLong')}</legend>
				<div class="durs" role="radiogroup" aria-label={t('profiles.adjust.howLong')} data-eq="durations">
					{#each DURATIONS as d (d.id)}
						<label class="dtile" class:on={duration === d.id}>
							<input type="radio" name="adj-duration" value={d.id} checked={duration === d.id} onchange={() => (duration = d.id)} data-testid="duration-{d.id}" />
							<span>{d.label}</span>
						</label>
					{/each}
				</div>
				{#if duration === 'date'}
					<label class="sr-only" for="adj-date">{t('profiles.adjust.until')}</label>
					<input id="adj-date" type="datetime-local" class="input mt-2" bind:value={date} data-testid="adjust-date" />
					{#if !dateValid}<p class="field-error" role="alert">{t('profiles.adjust.dateInvalid')}</p>{/if}
				{/if}
				<p class="t-help" data-testid="adjust-until">
					{#if end}
						{t('profiles.adjust.untilText', { date: formatWeekdayTime(end), value: formatValue(ruleKey, profileValue, ctx) })}
					{:else}
						{t('profiles.adjust.permanentText')}
					{/if}
				</p>
				{#if keyEnds}<p class="t-help warnline" data-testid="adjust-key-ends">{t('profiles.adjust.keyEnds', { date: formatWeekdayTime(end) })}</p>{/if}
				{#if existing && !existing.expires_at && end}<p class="t-help">{t('profiles.adjust.replacesPermanent')}</p>{/if}
			</fieldset>
			{#if isLoosening}
				<LooseningWarning rules={[ruleKey]} bind:confirmed subject={client?.name} />
			{/if}
			<label class="field">
				<span class="field-label">{t('profiles.adjust.note')}</span>
				<input class="input" maxlength={noteMax} bind:value={note} placeholder={t('profiles.adjust.notePlaceholder')} data-testid="adjust-note" />
			</label>
			{#if error}<Banner tone="error">{error}</Banner>{/if}
		</div>
	{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
			<button type="button" class="btn btn-primary" onclick={save} disabled={busy || dryPending || !dateValid || (isLoosening && !confirmed) || unchanged} data-testid="adjust-save">{busy ? t('common.saving') : t('profiles.adjust.save')}</button>
		</div>
	{/snippet}
</Modal>

<style>
	fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
	/* Equal sizes: six equal tiles, 3 × 2 (2 × 3 on phones). */
	.durs {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		grid-auto-rows: 1fr;
		gap: 8px;
	}
	.dtile {
		position: relative;
		display: grid;
		place-items: center;
		text-align: center;
		min-height: 48px;
		padding: 6px 8px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		font-weight: 650;
		cursor: pointer;
		min-width: 0;
	}
	.dtile span {
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 100%;
	}
	.dtile input {
		position: absolute;
		inset: 0;
		opacity: 0;
		margin: 0;
		cursor: pointer;
	}
	.dtile:focus-within {
		outline: 2px solid var(--focus);
		outline-offset: 1px;
	}
	.dtile.on {
		border-color: var(--ink);
		box-shadow: 0 0 0 1px var(--ink);
	}
	@media (max-width: 480px) {
		.durs {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
