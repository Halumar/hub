<script>
	/**
	 * "Effective rules" of a device: every catalog rule with its value
	 * and origin (profile, fixed, adjusted, temporary, boundary, …). Filter
	 * all / adjusted / fixed; rules that do not apply to the device are
	 * folded away. Actions: adjust (dialog with duration), reset to the
	 * profile value, or a link into the profile when the rule is fixed.
	 */
	import { base } from '$app/paths';
	import Icon from '$lib/ui/Icon.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import SourceBadge from '$lib/components/profiles/SourceBadge.svelte';
	import SceneHints from '$lib/components/profiles/SceneHints.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { groupsWithRules, L, catalog } from '$lib/profiles/catalog.svelte.js';
	import { formatValue } from '$lib/profiles/format.js';

	let { effective, ctx = {}, busyRule = '', onadjust, onreset } = $props();

	let filter = $state('all');
	let showNa = $state(false);

	const NA = ['not_applicable'];

	let rows = $derived.by(() => {
		const out = [];
		for (const g of groupsWithRules(catalog.data)) {
			const list = g.rules
				.map((r) => ({ rule: r, entry: effective?.rules?.[r.key] }))
				.filter((x) => x.entry);
			out.push({ group: g, list });
		}
		return out;
	});

	function isNa(e) {
		return NA.includes(e.source);
	}

	function matches(e) {
		if (isNa(e)) return false;
		if (filter === 'adjusted') return e.source === 'device';
		if (filter === 'fixed') return e.fixed && e.source !== 'device';
		return true;
	}

	let adjustedCount = $derived(Object.values(effective?.rules || {}).filter((e) => e.source === 'device').length);
	let naList = $derived(rows.flatMap((r) => r.list.filter((x) => isNa(x.entry))));

	function canAdjust(e) {
		return !e.fixed && !['boundary', 'prerequisite', 'access_expired', 'not_applicable'].includes(e.source);
	}

	// Scene hints: an area adjusted on the device is changed here, otherwise in the profile.
	let areasEntry = $derived(effective?.rules?.areas);
	let hintAdd = $derived(areasEntry?.source === 'device' && canAdjust(areasEntry) ? () => onadjust?.('areas') : null);
	let hintHref = $derived(effective?.profile && areasEntry?.source !== 'device' ? () => `${base}/profiles/${effective.profile.id}#r-areas` : null);
</script>

<section class="card card-flush" aria-labelledby="eff-h" data-testid="effective-rules">
	<div class="ehead">
		<div class="min-w-0">
			<h2 id="eff-h" class="t-card">{t('profiles.device.rulesTitle')}</h2>
			<p class="t-help">
				{t('profiles.device.rulesHint')}
				{#if adjustedCount}<span class="badge badge-warn ml-1" data-testid="adjusted-count"><Icon name="sliders" />{t('profiles.adjustedCount', { count: adjustedCount })}</span>{/if}
			</p>
		</div>
		<div class="filt">
			<RadioSeg
				size="even full"
				label={t('profiles.device.filter')}
				value={filter}
				options={[
					{ value: 'all', label: t('profiles.device.filterAll') },
					{ value: 'adjusted', label: t('profiles.device.filterAdjusted') },
					{ value: 'fixed', label: t('profiles.device.filterFixed') },
				]}
				onchange={(v) => (filter = v)}
				testid="rules-filter"
			/>
		</div>
	</div>
	{#if effective?.scene_hints?.length}
		<div class="pad hints">
			<SceneHints hints={effective.scene_hints} device areaName={ctx.areaName} onadd={hintAdd} addHref={hintHref} testid="device-scene-hints" />
		</div>
	{/if}
	{#each rows as { group, list } (group.id)}
		{@const visible = list.filter((x) => matches(x.entry))}
		{#if visible.length}
			<h3 class="t-overline gh">{L(group.label)}</h3>
			<ul class="rl">
				{#each visible as { rule, entry } (rule.key)}
					<li class="rr" class:adj={entry.source === 'device'} data-testid="eff-{rule.key}">
						<span class="lbl">{L(rule.label)}</span>
						<span class="val">{formatValue(rule.key, entry.value, ctx)}</span>
						<span class="src"><SourceBadge {entry} {rule} profileName={effective.profile?.name} platform={effective.platform} {ctx} testid="src-{rule.key}" /></span>
						<span class="act">
							<span class="sizer" aria-hidden="true"><span class="btn btn-sm"><Icon name="refresh" />{t('profiles.device.useProfile')}</span><span class="btn btn-sm"><Icon name="sliders" />{t('profiles.device.adjust')}</span><span class="btn btn-sm">{t('profiles.device.changeInProfile')}<Icon name="chev" /></span></span>
							{#if entry.source === 'device'}
								<button type="button" class="btn btn-sm" onclick={() => onreset?.(rule.key)} disabled={busyRule === rule.key} data-testid="reset-{rule.key}"><Icon name="refresh" /><span class="truncate">{t('profiles.device.useProfile')}</span></button>
							{:else if canAdjust(entry)}
								<button type="button" class="btn btn-sm" onclick={() => onadjust?.(rule.key)} data-testid="adjust-{rule.key}"><Icon name="sliders" /><span class="truncate">{t('profiles.device.adjust')}</span></button>
							{:else if entry.fixed && (entry.source === 'profile' || entry.source === 'default') && effective.profile}
								<a class="btn btn-sm btn-ghost" href="{base}/profiles/{effective.profile.id}#r-{rule.key}" data-testid="inprofile-{rule.key}"><span class="truncate">{t('profiles.device.changeInProfile')}</span><Icon name="chev" /></a>
							{/if}
						</span>
					</li>
				{/each}
			</ul>
		{/if}
	{/each}
	{#if filter !== 'all' && !rows.some((r) => r.list.some((x) => matches(x.entry)))}
		<p class="t-help pad" data-testid="rules-empty">{filter === 'adjusted' ? t('profiles.device.noneAdjusted') : t('profiles.device.noneFixed')}</p>
	{/if}
	{#if naList.length}
		<div class="pad na">
			<button type="button" class="btn btn-ghost btn-sm" aria-expanded={showNa ? 'true' : 'false'} onclick={() => (showNa = !showNa)} data-testid="na-toggle">
				<Icon name={showNa ? 'arrowup' : 'arrowdown'} />{t('profiles.device.notApplicable', { count: naList.length })}
			</button>
			{#if showNa}
				<ul class="rl">
					{#each naList as { rule, entry } (rule.key)}
						<li class="rr na-row">
							<span class="lbl">{L(rule.label)}</span>
							<span class="val">–</span>
							<span class="src"><SourceBadge {entry} {rule} platform={effective.platform} {ctx} /></span>
							<span class="act"><span class="sizer" aria-hidden="true"><span class="btn btn-sm"><Icon name="refresh" />{t('profiles.device.useProfile')}</span><span class="btn btn-sm"><Icon name="sliders" />{t('profiles.device.adjust')}</span><span class="btn btn-sm">{t('profiles.device.changeInProfile')}<Icon name="chev" /></span></span></span>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
</section>

<style>
	.ehead {
		display: flex;
		flex-wrap: wrap;
		gap: 10px 16px;
		align-items: flex-start;
		justify-content: space-between;
		padding: 14px 16px;
	}
	.ehead h2 {
		margin: 0;
	}
	.filt {
		width: min(100%, 360px);
	}
	.gh {
		margin: 0;
		padding: 8px 16px;
		background: var(--surface-2);
		border-top: 1px solid var(--line);
		border-bottom: 1px solid var(--line);
	}
	.rl {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.rr {
		display: grid;
		/* The action column is as wide as the longest action label in the
		   current language and font (every row holds the same sizer), so the
		   buttons are equal and never cut, whatever font the system uses. */
		grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1.4fr) max-content;
		gap: 6px 14px;
		align-items: center;
		padding: 10px 16px;
		border-bottom: 1px solid var(--line);
		min-height: 44px;
	}
	.rl:last-child .rr:last-child {
		border-bottom: 0;
	}
	.rr.adj {
		background: color-mix(in srgb, var(--warn-soft) 50%, var(--surface));
	}
	.lbl {
		font-weight: 600;
		min-width: 0;
	}
	.val {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.act {
		display: grid;
	}
	.act > * {
		grid-area: 1 / 1;
	}
	.act .btn {
		width: 100%;
		justify-content: center;
		min-width: 0;
	}
	/* Width only: the three action labels stacked, invisible and without height. */
	.sizer {
		display: grid;
		visibility: hidden;
		height: 0;
		overflow: hidden;
		pointer-events: none;
	}
	.sizer > .btn {
		grid-area: 1 / 1;
		width: auto;
	}
	.pad {
		padding: 12px 16px;
	}
	.hints {
		border-top: 1px solid var(--line);
	}
	.na .rl {
		margin-top: 6px;
	}
	.na-row {
		color: var(--ink-2);
	}
	@media (max-width: 1100px) {
		.rr {
			grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		}
		.src {
			grid-column: 1;
		}
		.act {
			grid-column: 2;
		}
	}
	@media (max-width: 560px) {
		.rr {
			grid-template-columns: minmax(0, 1fr);
		}
		.src,
		.act {
			grid-column: auto;
		}
	}
</style>
