<script>
	/**
	 * Scenes and scripts with targets the profile (or device) may not switch:
	 * one sentence per scene and reason, e.g. "Guten Morgen" switches
	 * "Kitchen light" – not in the rooms of this profile. The integration
	 * refuses the scene for these devices. Actions add the target's area:
	 * `onadd(areaId)` (button, editor draft) or `addHref(areaId)` (link).
	 */
	import { base } from '$app/paths';
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { hintLines, areasToAdd } from '$lib/profiles/scene-hints.js';

	let {
		hints = [],
		device = false,
		areaName = (id) => id,
		onadd = null,
		addHref = null,
		currentAreaId = '',
		title = '',
		testid = 'scene-hints',
	} = $props();

	const REASON = {
		outside_areas: 'outside',
		not_controllable: 'notControllable',
		kids_blocked: 'kidsBlocked',
		remote: 'remote',
	};

	function kind(h) {
		return h.entity_id?.startsWith('script.') ? 'Script' : 'Scene';
	}

	function quoted(targets) {
		return targets.map((x) => t('profiles.sceneHints.quoted', { name: x.name })).join(', ');
	}

	function sentence(h, line) {
		let what = REASON[line.reason];
		if (line.reason === 'outside_areas' && device) what = 'outsideDevice';
		const lead = t('profiles.sceneHints.switches', { name: h.name, targets: quoted(line.targets) });
		const reason = t(`profiles.sceneHints.${what}`, { count: line.targets.length });
		const refused =
			line.reason === 'remote'
				? t(`profiles.sceneHints.refused${kind(h)}Away`)
				: t(`profiles.sceneHints.refused${kind(h)}${device ? 'Device' : ''}`);
		return `${lead} – ${reason}. ${refused}`;
	}

	/** Targets outside the profile and in no area: add them to the scene's area. */
	function homeless(h, line) {
		if (line.reason !== 'outside_areas') return null;
		if (!line.targets.some((x) => !x.area_ids?.length)) return null;
		const area = h.area_ids?.[0];
		return area && area !== currentAreaId ? area : null;
	}
</script>

{#if hints?.length}
	<section class="sh" aria-labelledby="{testid}-h" data-testid={testid}>
		<div class="shead">
			<span class="sic" aria-hidden="true"><Icon name="scene" size="16" /></span>
			<h3 id="{testid}-h" class="stitle">{title || t('profiles.sceneHints.title')}</h3>
		</div>
		<ul class="slist">
			{#each hints as h (h.entity_id)}
				{#each hintLines(h) as line (line.reason)}
					{@const adds = line.reason === 'outside_areas' ? areasToAdd(line.targets) : []}
					{@const home = homeless(h, line)}
					<li class="srow" data-testid="{testid}-row" data-entity={h.entity_id} data-reason={line.reason}>
						<p class="stext" data-testid="{testid}-text">{sentence(h, line)}</p>
						{#if (adds.length && (onadd || addHref)) || home}
							<div class="sact">
								{#each adds as a (a)}
									{#if onadd}
										<button type="button" class="btn btn-sm" onclick={() => onadd(a)} title={t('profiles.sceneHints.addArea', { area: areaName(a) })} data-testid="{testid}-add"><Icon name="plus" /><span class="truncate">{t('profiles.sceneHints.addArea', { area: areaName(a) })}</span></button>
									{:else if addHref?.(a)}
										<a class="btn btn-sm" href={addHref(a)} title={t('profiles.sceneHints.addAreaInProfile', { area: areaName(a) })} aria-label={t('profiles.sceneHints.addAreaInProfile', { area: areaName(a) })} data-testid="{testid}-add"><span class="truncate">{t('profiles.sceneHints.addArea', { area: areaName(a) })}</span><Icon name="chev" /></a>
									{/if}
								{/each}
								{#if home}
									<a class="btn btn-sm" href="{base}/areas/{home}" title={t('profiles.sceneHints.toAreaLong', { area: areaName(home) })} aria-label={t('profiles.sceneHints.toAreaLong', { area: areaName(home) })} data-testid="{testid}-to-area"><span class="truncate">{t('profiles.sceneHints.toArea', { area: areaName(home) })}</span><Icon name="chev" /></a>
								{/if}
							</div>
						{/if}
					</li>
				{/each}
				{#if h.targets_unknown}
					<li class="srow" data-testid="{testid}-row" data-entity={h.entity_id} data-reason="unknown">
						<p class="stext" data-testid="{testid}-text">{t(`profiles.sceneHints.unknown${kind(h)}`, { name: h.name })}</p>
					</li>
				{/if}
			{/each}
		</ul>
	</section>
{/if}

<style>
	.sh {
		display: grid;
		gap: 8px;
		background: var(--surface-2);
		border-radius: var(--r-box);
		padding: 12px 14px;
		min-width: 0;
	}
	.shead {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.sic {
		display: inline-grid;
		place-items: center;
		width: 28px;
		height: 28px;
		flex: none;
		border-radius: 50%;
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.stitle {
		margin: 0;
		font-size: var(--fs-body);
		font-weight: 650;
	}
	.slist {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 8px;
	}
	.srow {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 6px 14px;
		align-items: center;
	}
	.stext {
		margin: 0;
		color: var(--ink-2);
		overflow-wrap: anywhere;
	}
	.sact {
		display: grid;
		grid-template-columns: 14rem;
		gap: 6px;
	}
	.sact .btn {
		width: 100%;
		min-width: 0;
		justify-content: center;
	}
	@media (max-width: 640px) {
		.srow {
			grid-template-columns: minmax(0, 1fr);
		}
		.sact {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
