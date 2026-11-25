<script>
	/**
	 * Change several devices together: assign a profile, set the own
	 * area, adjust a rule (permanent or temporary) or remove an adjustment.
	 * Choose → preview (dry run on the hub: per device changed / unchanged /
	 * skipped with reason, changed values, loosened rules) → apply → result.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import Banner from '$lib/ui/Banner.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { BULK_ACTIONS, bulkBody, counts, emptyChoices } from '$lib/bulk.js';
	import Modal from '$lib/ui/Modal.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import ProfilePicker from '$lib/components/profiles/ProfilePicker.svelte';
	import RuleValueEditor from '$lib/components/profiles/RuleValueEditor.svelte';
	import PreviewView from '$lib/components/profiles/PreviewView.svelte';
	import LooseningWarning from '$lib/components/profiles/LooseningWarning.svelte';
	import { loadCatalog, catalog, L, expiryPresets, ruleOf, KEY_PLATFORMS } from '$lib/profiles/catalog.svelte.js';
	import { loadRuleContext, areaNamer } from '$lib/profiles/context.js';
	import { clone } from '$lib/profiles/format.js';
	import { errorText } from '$lib/profiles/errors.js';

	let { open = $bindable(false), clients = [], ondone = () => {} } = $props();

	let step = $state('edit'); // edit | review | done
	let choices = $state(emptyChoices());
	let preview = $state(null);
	let result = $state(null);
	let busy = $state(false);
	let error = $state('');
	let confirmed = $state(false);
	let statusEl = $state(null);
	let profiles = $state([]);
	let ref = $state({ areas: [], tags: [], entities: [] });

	const PREVIEW_NAMES = 6;

	onMount(() => {
		loadCatalog().catch(() => {});
		loadRuleContext().then((r) => (ref = r));
	});

	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) {
			step = 'edit';
			choices = emptyChoices();
			preview = null;
			result = null;
			error = '';
			confirmed = false;
			api.listProfiles().then((r) => (profiles = r?.profiles || [])).catch(() => (profiles = []));
		}
		wasOpen = open;
	});

	let ids = $derived(clients.map((c) => c.id));
	let guests = $derived(clients.filter((c) => c.access === 'guest').length);
	let body = $derived(bulkBody(choices, ids, true));
	let shown = $derived(step === 'done' ? result : preview);
	let kpi = $derived(counts(shown));
	let names = $derived(Object.fromEntries(clients.map((c) => [c.id, c.name])));
	let adjustable = $derived((catalog.data?.rules || []).filter((r) => r.fixed_checkbox));
	let rule = $derived(ruleOf(choices.rule));
	// TVs and watches never run without a key: "Hub only" is not offered while one is selected.
	let keyDevices = $derived(clients.some((c) => KEY_PLATFORMS.includes(c.platform)));
	let ctx = $derived({ ...ref, areaName: areaNamer(ref.areas), hiddenChoices: keyDevices ? { control_path: ['hub'] } : {} });
	let loosened = $derived(preview?.preview?.loosened_rules || []);
	let durations = $derived([...expiryPresets().map((p) => ({ id: p.id, label: L(p.label) })), { id: 'date', label: t('profiles.adjust.date') }, { id: 'permanent', label: t('profiles.adjust.permanent') }]);

	function setAction(a) {
		choices = { ...emptyChoices(), action: a };
	}

	function setRule(key) {
		const r = ruleOf(key);
		choices = { ...choices, rule: key, value: r ? clone(r.default) : null };
	}

	function focusStatus() {
		requestAnimationFrame(() => statusEl?.focus({ preventScroll: false }));
	}

	// The preview belongs to the request it was asked for: an answer for inputs changed meanwhile is
	// dropped, and applying needs the preview of exactly the current request.
	let previewBody = $state('');
	let bodyKey = $derived(JSON.stringify(body));

	async function runPreview() {
		if (!body || busy) return;
		const asked = bodyKey;
		busy = true;
		error = '';
		confirmed = false;
		try {
			const pv = await api.bulkClients(body);
			if (asked !== bodyKey) return;
			preview = pv;
			previewBody = asked;
			step = 'review';
			focusStatus();
		} catch (e) {
			if (asked === bodyKey) error = t('bulk.previewFailed', { message: errorText(e) });
		} finally {
			busy = false;
		}
	}

	async function apply() {
		if (!body || busy) return;
		busy = true;
		error = '';
		try {
			result = await api.bulkClients({ ...body, dry_run: false });
			step = 'done';
			focusStatus();
			ondone({ ...counts(result) });
		} catch (e) {
			error = t('bulk.failed', { message: errorText(e) });
		} finally {
			busy = false;
		}
	}

	function guestReason(p) {
		return guests && p.child_protection ? t('bulk.guestChildSkipped') : '';
	}
</script>

<Modal
	bind:open
	size="lg"
	testid="bulk-dialog"
	dismissible={!busy}
	title={step === 'edit' ? (clients.length === 1 ? t('bulk.titleOne') : t('bulk.title', { count: clients.length })) : step === 'review' ? t('bulk.reviewTitle') : t('bulk.resultTitle')}
	description={step === 'edit' ? t('bulk.intro') : ''}
>
	{#if step === 'edit'}
		<div class="stack">
			<section aria-labelledby="bulk-devices-h" class="box devices">
				<div class="row justify-between">
					<h3 id="bulk-devices-h" class="t-label">{t('bulk.devicesLabel')}</h3>
					<span class="t-help">{t('bulk.membersGuests', { members: clients.length - guests, guests })}</span>
				</div>
				<p class="names" data-testid="bulk-names">
					{clients.slice(0, PREVIEW_NAMES).map((c) => c.name).join(', ')}{#if clients.length > PREVIEW_NAMES}{' '}<span class="t-help">{t('bulk.devicesMore', { count: clients.length - PREVIEW_NAMES })}</span>{/if}
				</p>
			</section>

			<div class="field">
				<span class="field-label" id="bulk-action-l">{t('bulk.actionLabel')}</span>
				<RadioSeg size="even full two" labelledby="bulk-action-l" value={choices.action} options={BULK_ACTIONS.map((a) => ({ value: a, label: t(`bulk.actions.${a}`) }))} onchange={setAction} testid="bulk-action" />
				<p class="t-help">{t(`bulk.actionHint.${choices.action}`)}</p>
			</div>

			{#if choices.action === 'assign_profile'}
				<ProfilePicker {profiles} bind:value={choices.profileId} label={t('bulk.profile')} disabledReason={guestReason} name="bulk-profile" testid="bulk-profile" />
				{#if guests && profiles.some((p) => p.child_protection)}<p class="t-help">{t('bulk.guestChildHint')}</p>{/if}
				<label class="check"><input type="checkbox" bind:checked={choices.discard} data-testid="bulk-discard" />{t('bulk.discard')}</label>
			{:else if choices.action === 'set_own_area'}
				<label class="field">
					<span class="field-label">{t('profiles.device.ownArea')}</span>
					<select class="select" bind:value={choices.areaId} data-testid="bulk-area">
						<option value="">{t('profiles.device.noOwnArea')}</option>
						{#each ref.areas as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
					</select>
					<span class="t-help">{t('profiles.device.ownAreaHint')}</span>
				</label>
			{:else}
				<label class="field">
					<span class="field-label">{t('bulk.rule')}</span>
					<select class="select" value={choices.rule} onchange={(e) => setRule(e.currentTarget.value)} data-testid="bulk-rule">
						<option value="">{t('bulk.rulePick')}</option>
						{#each adjustable as r (r.key)}<option value={r.key}>{L(r.label)}</option>{/each}
					</select>
					<span class="t-help">{t('bulk.ruleHint')}</span>
				</label>
				{#if choices.action === 'set_override' && rule}
					<div class="field">
						<span class="field-label">{t('profiles.adjust.newValue')}</span>
						<RuleValueEditor {rule} bind:value={choices.value} {ctx} idPrefix="bulk" />
						{#if rule.key === 'control_path' && keyDevices}<p class="t-help" data-testid="bulk-no-hub">{t('bulk.noHubForKeyDevices')}</p>{/if}
					</div>
					<fieldset class="field">
						<legend class="field-label mb-1.5">{t('profiles.adjust.howLong')}</legend>
						<div class="durs" role="radiogroup" aria-label={t('profiles.adjust.howLong')}>
							{#each durations as d (d.id)}
								<label class="dtile" class:on={choices.duration === d.id}>
									<input type="radio" name="bulk-duration" checked={choices.duration === d.id} onchange={() => (choices.duration = d.id)} />
									<span>{d.label}</span>
								</label>
							{/each}
						</div>
						{#if choices.duration === 'date'}
							<label class="sr-only" for="bulk-date">{t('profiles.adjust.until')}</label>
							<input id="bulk-date" type="datetime-local" class="input mt-2" bind:value={choices.date} />
						{/if}
					</fieldset>
					<label class="field">
						<span class="field-label">{t('profiles.adjust.note')}</span>
						<input class="input" maxlength="200" bind:value={choices.note} placeholder={t('profiles.adjust.notePlaceholder')} />
					</label>
				{/if}
			{/if}
		</div>
	{:else if shown}
		<div class="stack">
			<p class="sr-only" tabindex="-1" bind:this={statusEl} role="status" data-testid="bulk-status">
				{t(step === 'done' ? 'bulk.resultLive' : 'bulk.summaryLive', kpi)}
			</p>
			<div class="kpis" data-testid="bulk-kpis">
				{#each ['changed', 'unchanged', 'skipped'] as k (k)}
					<div class="box kpi" class:hl={k === 'changed' && kpi.changed > 0} class:warnk={k === 'skipped' && kpi.skipped > 0}>
						<span class="t-help">{t(step === 'done' ? `bulk.kpiDone.${k}` : `bulk.kpi.${k}`)}</span>
						<b data-testid="bulk-kpi-{k}">{kpi[k]}</b>
					</div>
				{/each}
			</div>
			<section aria-labelledby="bulk-per-device-h">
				<h3 id="bulk-per-device-h" class="t-label mb-1">{t('bulk.perDevice')}</h3>
				<ul class="list results" data-testid="bulk-results">
					{#each shown.results || [] as r (r.client_id)}
						<li>
							<span class="min-w-0 flex-1">
								<b class="rname">{r.name || names[r.client_id] || r.client_id}</b>
								{#if r.reason}<span class="t-help">{t(`bulk.reason.${r.reason}`)}</span>{/if}
							</span>
							<span class="badge st st-{r.status}">{t(step === 'done' ? `bulk.statusDone.${r.status}` : `bulk.status.${r.status}`)}</span>
						</li>
					{/each}
				</ul>
			</section>
			{#if step === 'review' && previewBody !== bodyKey}
				<Banner tone="warn" testid="bulk-preview-stale">
					{t('bulk.previewStale')}
					{#snippet actions()}<button type="button" class="btn btn-sm" onclick={runPreview} disabled={busy} data-testid="bulk-preview-refresh">{t('bulk.previewRefresh')}</button>{/snippet}
				</Banner>
			{/if}
			{#if step === 'review' && preview?.preview}
				<PreviewView preview={preview.preview} {ctx} testid="bulk-preview-detail" />
				<LooseningWarning rules={loosened} bind:confirmed />
			{/if}
			{#if step === 'done'}<p class="t-help">{t('bulk.auditNote')}</p>{/if}
		</div>
	{/if}

	{#if error}<p class="err mt-3" role="alert" data-testid="bulk-error">{error}</p>{/if}

	{#snippet footer({ close })}
		<div class="foot-eq">
			{#if step === 'edit'}
				<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
				<button type="button" class="btn btn-primary" onclick={runPreview} disabled={busy || !body} data-testid="bulk-preview">{busy ? t('bulk.previewing') : t('bulk.preview')}</button>
			{:else if step === 'review'}
				<button type="button" class="btn" onclick={() => (step = 'edit')} disabled={busy}>{t('bulk.back')}</button>
				<button type="button" class="btn btn-primary" onclick={apply} disabled={busy || previewBody !== bodyKey || !kpi.changed || (loosened.length > 0 && !confirmed)} data-testid="bulk-apply">
					{busy ? t('bulk.applying') : kpi.changed ? t('bulk.apply', { count: kpi.changed }) : t('bulk.applyNone')}
				</button>
			{:else}
				<button type="button" class="btn btn-primary" onclick={close} data-testid="bulk-done">{t('bulk.done')}</button>
			{/if}
		</div>
	{/snippet}
</Modal>

<style>
	.devices .names {
		margin-top: 4px;
		overflow-wrap: anywhere;
	}
	fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
	/* Four actions: 4 equal columns, 2 × 2 on phones. */
	:global(.seg.even.full.two) {
		grid-template-columns: repeat(4, minmax(0, 1fr));
		grid-auto-flow: row;
	}
	@media (max-width: 639px) {
		:global(.seg.even.full.two) {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
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
		min-height: 44px;
		padding: 6px 8px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		font-weight: 650;
		cursor: pointer;
		min-width: 0;
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
	}
	.dtile.on {
		border-color: var(--ink);
		box-shadow: 0 0 0 1px var(--ink);
	}
	.kpis {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
	}
	.kpis .kpi span {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.kpis .kpi b {
		font-size: 20px;
	}
	.kpi.hl b {
		color: var(--ok-ink, var(--ok));
	}
	.kpi.warnk b {
		color: var(--warn-ink);
	}
	.results {
		max-height: 320px;
		overflow: auto;
	}
	.results li {
		gap: 10px;
		align-items: center;
	}
	.rname {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.st {
		flex: none;
		min-width: 7.5rem;
		justify-content: center;
	}
	.st-changed {
		background: var(--ok-soft);
		color: var(--ok-ink);
	}
	.st-skipped {
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.err {
		color: var(--err-ink);
	}
	@media (max-width: 480px) {
		.durs {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
</style>
