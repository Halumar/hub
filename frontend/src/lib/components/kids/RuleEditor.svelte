<script>
	/**
	 * Rule editor of the kids mode for one level: profile, area or
	 * entity. Below the profile every building block is either inherited
	 * (shows the inherited value) or overridden ("Own rule").
	 *
	 *   <RuleEditor level="area" rules={…} inherited={profile.rules} types={…} onsave={(rules) => …} />
	 *
	 * onsave(rules) returns a Promise; a rejected 422 "rules.<field>" marks the
	 * field. onremove (area/entity) removes the whole rule object.
	 */
	import Switch from '$lib/ui/Switch.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import QuietHoursEditor from './QuietHoursEditor.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { DEFAULT_RATE, clone, levelKeys, rateValid, ruleErrorField, summarize, typeLabel, windowsValid } from '$lib/data/kids.js';

	let {
		level = 'profile',
		rules = {},
		inherited = null,
		types = [],
		onsave,
		onremove = null,
		oncancel = null,
		idPrefix = 'rules',
		testid = 'rule-editor',
	} = $props();

	const isProfile = $derived(level === 'profile');
	const keys = $derived(levelKeys(level));

	let draft = $state({});
	let saving = $state(false);
	let errorField = $state('');
	let errorText = $state('');
	let baseline = $state('{}');

	$effect(() => {
		reset(rules);
	});

	function prepare(src) {
		const next = clone(src || {}) || {};
		if (level === 'profile') {
			next.blocked ??= false;
			next.blocked_types ??= [];
			next.quiet_hours ??= [];
			if (!('rate_limit' in next)) next.rate_limit = null;
			next.color_lock ??= false;
			if (!('volume_max' in next)) next.volume_max = null;
		}
		return next;
	}

	function reset(src) {
		const next = prepare(src);
		draft = next;
		baseline = JSON.stringify(canonical(next));
		errorField = '';
		errorText = '';
	}

	function canonical(obj) {
		const out = {};
		for (const k of levelKeys(level)) if (obj && k in obj) out[k] = obj[k];
		return out;
	}

	let dirty = $derived(JSON.stringify(canonical(draft)) !== baseline);
	let valid = $derived(windowsValid(draft.quiet_hours) && rateValid(draft.rate_limit) && (draft.volume_max == null || (Number.isInteger(draft.volume_max) && draft.volume_max >= 0 && draft.volume_max <= 100)));

	function own(key) {
		return Object.prototype.hasOwnProperty.call(draft, key);
	}

	function inheritedValue(key) {
		if (!inherited) return undefined;
		return inherited[key];
	}

	function setOwn(key, on) {
		const next = { ...draft };
		if (on) {
			const inh = clone(inheritedValue(key));
			next[key] = defaultFor(key, inh);
		} else {
			delete next[key];
		}
		draft = next;
		return true;
	}

	function defaultFor(key, inh) {
		switch (key) {
			case 'blocked':
				return true;
			case 'blocked_types':
				return inh ?? [];
			case 'quiet_hours':
				return inh ?? [];
			case 'rate_limit':
				return inh === undefined ? clone(DEFAULT_RATE) : inh;
			case 'color_lock':
				return !(inh ?? false);
			case 'volume_max':
				return inh === undefined ? 40 : inh;
			default:
				return null;
		}
	}

	function toggleType(type) {
		const list = draft.blocked_types || [];
		draft.blocked_types = list.includes(type) ? list.filter((x) => x !== type) : [...list, type];
	}

	let allTypes = $derived([...new Set([...(types || []), ...(draft.blocked_types || []), ...((inherited && inherited.blocked_types) || [])])]);

	function setRateOn(on) {
		draft.rate_limit = on ? clone(DEFAULT_RATE) : null;
		return true;
	}

	function setVolumeOn(on) {
		draft.volume_max = on ? 40 : null;
		return true;
	}

	function intOf(v) {
		const n = Number(v);
		return Number.isFinite(n) ? Math.round(n) : NaN;
	}

	async function save() {
		if (!valid || saving) return;
		saving = true;
		errorField = '';
		errorText = '';
		try {
			await onsave?.(canonical(draft));
			baseline = JSON.stringify(canonical(draft));
		} catch (e) {
			errorField = ruleErrorField(e) || '';
			errorText = errorField ? t('kids.editor.fieldError', { field: t(`kids.block.${errorField}`) }) : e.message;
		} finally {
			saving = false;
		}
	}

	function cancel() {
		reset(rules);
		oncancel?.();
	}
</script>

{#snippet head(key, icon)}
	<div class="bhead">
		<Icon name={icon} />
		<h3 class="t-card flex-1" id="{idPrefix}-{key}-h">{t(`kids.block.${key}`)}</h3>
		{#if !isProfile}
			<Switch checked={own(key)} label={t('kids.editor.own')} onchange={(v) => setOwn(key, v)} testid="own-{key}" class="own" />
		{/if}
	</div>
	{#if !isProfile && !own(key)}
		<p class="t-help" data-testid="inherited-{key}">{t('kids.editor.inherited', { value: summarize(key, inheritedValue(key)) })}</p>
	{/if}
{/snippet}

<div class="re" data-testid={testid} data-level={level}>
	{#if keys.includes('blocked')}
		<section class="block" class:err={errorField === 'blocked'} aria-labelledby="{idPrefix}-blocked-h" data-testid="block-blocked">
			{@render head('blocked', 'lock')}
			{#if isProfile}
				<Switch checked={!!draft.blocked} label={t('kids.access.blockAll')} description={t('kids.access.blockAllHint')} onchange={(v) => ((draft.blocked = v), true)} testid="blocked-all" />
			{:else if own('blocked')}
				<RadioSeg
					label={t('kids.block.blocked')}
					value={draft.blocked ? 'blocked' : 'allowed'}
					options={[
						{ value: 'blocked', label: t('kids.access.blocked') },
						{ value: 'allowed', label: t('kids.access.allowed') },
					]}
					onchange={(v) => (draft.blocked = v === 'blocked')}
				/>
				<p class="t-help">{draft.blocked ? t('kids.access.blockedHint') : t('kids.access.allowedHint')}</p>
			{/if}
		</section>
	{/if}

	{#if keys.includes('blocked_types')}
		<section class="block" class:err={errorField === 'blocked_types'} aria-labelledby="{idPrefix}-blocked_types-h" data-testid="block-blocked_types">
			{@render head('blocked_types', 'shield')}
			{#if isProfile || own('blocked_types')}
				<p class="t-help">{t('kids.types.hint')}</p>
				<div class="types" role="group" aria-labelledby="{idPrefix}-blocked_types-h">
					{#each allTypes as type (type)}
						<button type="button" class="chip tchip" aria-pressed={draft.blocked_types?.includes(type) ? 'true' : 'false'} title={typeLabel(type)} onclick={() => toggleType(type)} data-testid="type-{type}">
							{#if draft.blocked_types?.includes(type)}<Icon name="lock" size="14" />{/if}
							<span class="truncate">{typeLabel(type)}</span>
						</button>
					{/each}
				</div>
			{/if}
		</section>
	{/if}

	{#if keys.includes('quiet_hours')}
		<section class="block" class:err={errorField === 'quiet_hours'} aria-labelledby="{idPrefix}-quiet_hours-h" data-testid="block-quiet_hours">
			{@render head('quiet_hours', 'moon')}
			{#if isProfile || own('quiet_hours')}
				<p class="t-help">{t('kids.quiet.hint')}</p>
				<QuietHoursEditor bind:windows={draft.quiet_hours} idPrefix="{idPrefix}-qh" />
			{/if}
		</section>
	{/if}

	{#if keys.includes('rate_limit')}
		<section class="block" class:err={errorField === 'rate_limit'} aria-labelledby="{idPrefix}-rate_limit-h" data-testid="block-rate_limit">
			{@render head('rate_limit', 'clock')}
			{#if isProfile || own('rate_limit')}
				<Switch checked={!!draft.rate_limit} label={draft.rate_limit ? t('kids.rate.on') : t('kids.rate.offLabel')} description={t('kids.rate.hint')} onchange={setRateOn} testid="rate-on" />
				{#if draft.rate_limit}
					<div class="nums">
						<label class="nfield">
							<span class="t-label">{t('kids.rate.max')}</span>
							<input type="number" class="input" min="1" max="100" step="1" value={draft.rate_limit.max_actions} oninput={(e) => (draft.rate_limit = { ...draft.rate_limit, max_actions: intOf(e.currentTarget.value) })} data-testid="rate-max" />
						</label>
						<label class="nfield">
							<span class="t-label">{t('kids.rate.window')}</span>
							<input type="number" class="input" min="5" max="3600" step="5" value={draft.rate_limit.window_seconds} oninput={(e) => (draft.rate_limit = { ...draft.rate_limit, window_seconds: intOf(e.currentTarget.value) })} data-testid="rate-window" />
						</label>
						<label class="nfield">
							<span class="t-label">{t('kids.rate.pause')}</span>
							<input type="number" class="input" min="0" max="1440" step="1" value={Math.round(draft.rate_limit.cooldown_seconds / 60)} oninput={(e) => (draft.rate_limit = { ...draft.rate_limit, cooldown_seconds: intOf(e.currentTarget.value) * 60 })} data-testid="rate-pause" />
						</label>
					</div>
					{#if !rateValid(draft.rate_limit)}<p class="t-err text-sm">{t('kids.rate.invalid')}</p>{/if}
					<p class="t-help">{summarize('rate_limit', draft.rate_limit)}</p>
				{/if}
			{/if}
		</section>
	{/if}

	{#if keys.includes('color_lock')}
		<section class="block" class:err={errorField === 'color_lock'} aria-labelledby="{idPrefix}-color_lock-h" data-testid="block-color_lock">
			{@render head('color_lock', 'palette')}
			{#if isProfile || own('color_lock')}
				<Switch checked={!!draft.color_lock} label={draft.color_lock ? t('kids.color.on') : t('kids.color.off')} description={t('kids.color.hint')} onchange={(v) => ((draft.color_lock = v), true)} testid="color-lock" />
			{/if}
		</section>
	{/if}

	{#if keys.includes('volume_max')}
		<section class="block" class:err={errorField === 'volume_max'} aria-labelledby="{idPrefix}-volume_max-h" data-testid="block-volume_max">
			{@render head('volume_max', 'volume')}
			{#if isProfile || own('volume_max')}
				<Switch checked={draft.volume_max != null} label={draft.volume_max != null ? t('kids.volume.on') : t('kids.volume.off')} description={t('kids.volume.hint')} onchange={setVolumeOn} testid="volume-on" />
				{#if draft.volume_max != null}
					<div class="vol">
						<input type="range" min="0" max="100" step="5" value={draft.volume_max} oninput={(e) => (draft.volume_max = intOf(e.currentTarget.value))} aria-label={t('kids.volume.max')} />
						<label class="nfield vnum">
							<span class="sr-only">{t('kids.volume.max')}</span>
							<input type="number" class="input" min="0" max="100" value={draft.volume_max} oninput={(e) => (draft.volume_max = intOf(e.currentTarget.value))} data-testid="volume-max" />
						</label>
						<span class="t-muted">%</span>
					</div>
				{/if}
			{/if}
		</section>
	{/if}

	{#if errorText}<p class="t-err" role="alert" data-testid="rule-error">{errorText}</p>{/if}

	<div class="actions">
		<button type="button" class="btn btn-primary" onclick={save} disabled={!dirty || !valid || saving} data-testid="rules-save">{saving ? t('common.saving') : t('common.save')}</button>
		<button type="button" class="btn" onclick={cancel} disabled={saving || (!dirty && !oncancel)} data-testid="rules-cancel">{t('common.cancel')}</button>
		{#if onremove}
			<span class="spacer"></span>
			<button type="button" class="btn btn-danger" onclick={onremove} disabled={saving} data-testid="rules-remove">{t('kids.editor.removeAll')}</button>
		{/if}
	</div>
</div>

<style>
	.re {
		display: grid;
		gap: 12px;
		min-width: 0;
	}
	.block {
		display: grid;
		gap: 8px;
		padding: 12px 14px;
		border: 1px solid var(--line);
		border-radius: var(--r-box);
		background: var(--surface);
		min-width: 0;
	}
	.block.err {
		border-color: var(--err);
	}
	.bhead {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	/* Equal sizes: equal type buttons in a grid. */
	.types {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 176px), 1fr));
		gap: 8px;
	}
	.tchip {
		justify-content: flex-start;
		min-width: 0;
		min-height: var(--h-ctl);
		overflow: hidden;
	}
	.nums {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
	}
	.nfield {
		display: grid;
		gap: 4px;
		min-width: 0;
		align-content: end;
	}
	.nfield .input {
		width: 100%;
		min-width: 0;
	}
	.vol {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 84px auto;
		gap: 10px;
		align-items: center;
	}
	.vol input[type='range'] {
		width: 100%;
		accent-color: var(--accent);
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
	}
	@media (max-width: 639px) {
		.nums {
			grid-template-columns: minmax(0, 1fr);
		}
		.types {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
