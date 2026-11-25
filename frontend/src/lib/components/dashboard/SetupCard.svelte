<script>
	/**
	 * Setup checklist of the overview. Open while something is missing; once
	 * everything is done it shrinks to one line ("Setup complete") and can be
	 * opened and closed by hand (remembered in localStorage "hub.dash.setup").
	 */
	import { base } from '$app/paths';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { checklist, resumable = false, class: cls = '' } = $props();

	const KEY = 'hub.dash.setup';

	function readPref() {
		try {
			return localStorage.getItem(KEY) === 'open';
		} catch {
			return false;
		}
	}

	let openByHand = $state(readPref());
	let doneCount = $derived(checklist.filter((i) => i.done).length);
	let complete = $derived(doneCount === checklist.length);
	let expanded = $derived(!complete || openByHand);

	function toggle() {
		openByHand = !expanded;
		try {
			if (openByHand) localStorage.setItem(KEY, 'open');
			else localStorage.removeItem(KEY);
		} catch {
			/* private mode: only for this visit */
		}
	}
</script>

<section
	class="card setup {cls}"
	class:slim={!expanded}
	aria-labelledby="dash-setup"
	data-testid="dash-setup"
	data-state={expanded ? 'open' : 'collapsed'}
>
	<div class="card-head">
		{#if complete}
			<h2 id="dash-setup" class="flex-1 min-w-0">
				<button type="button" class="disclosure" aria-expanded={expanded} aria-controls="dash-setup-body" onclick={toggle} data-testid="dash-setup-toggle">
					<span class="okmark"><Icon name="check" /></span>
					<span class="label">{expanded ? t('dashboard.checklist.title', { done: doneCount, total: checklist.length }) : t('dashboard.checklist.complete')}</span>
					<Icon name="chevdown" class="chev" />
				</button>
			</h2>
			<a class="btn btn-sm assist" href="{base}/setup" title={t('dashboard.checklist.openAssistant')} data-testid="open-setup">
				<Icon name="sparkle" /><span class="assist-label">{t('dashboard.checklist.openAssistant')}</span>
			</a>
		{:else}
			<Icon name="check" />
			<h2 id="dash-setup" class="flex-1">{t('dashboard.checklist.title', { done: doneCount, total: checklist.length })}</h2>
			<a class="btn btn-sm" class:btn-primary={resumable} href="{base}/setup" data-testid="open-setup">
				<Icon name="sparkle" />{resumable ? t('setup.dashboard.resume') : t('setup.dashboard.start')}
			</a>
		{/if}
	</div>
	{#if expanded}
		<div id="dash-setup-body">
			<div class="progress mb-2" role="progressbar" aria-label={t('dashboard.checklist.progress')} aria-valuemin="0" aria-valuemax={checklist.length} aria-valuenow={doneCount}>
				<i style:width="{(doneCount / checklist.length) * 100}%"></i>
			</div>
			<ul class="list" data-testid="setup-checklist">
				{#each checklist as item (item.key)}
					<li>
						<span class="tick" class:done={item.done}>
							{#if item.done}<Icon name="check" />{/if}
							<span class="sr-only">{item.done ? t('dashboard.checklist.done') : t('dashboard.checklist.open')}</span>
						</span>
						<span class="flex-1">{t(`dashboard.checklist.${item.key}`)}</span>
						{#if !item.done}
							{#if item.href}
								<a class="btn btn-sm" href={item.href}>{t(`dashboard.checklist.do.${item.key}`)}</a>
							{:else}
								<button type="button" class="btn btn-sm" onclick={item.action}>{t(`dashboard.checklist.do.${item.key}`)}</button>
							{/if}
						{/if}
					</li>
				{/each}
			</ul>
		</div>
	{/if}
</section>

<style>
	.setup.slim {
		padding-top: 10px;
		padding-bottom: 10px;
	}
	.setup.slim .card-head {
		margin-bottom: 0;
	}
	.disclosure {
		display: flex;
		align-items: center;
		gap: var(--s2);
		max-width: 100%;
		min-height: 36px;
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
		border-radius: var(--r-sm);
	}
	.disclosure:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}
	.disclosure:hover .label {
		text-decoration: underline;
	}
	.label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.okmark {
		width: 22px;
		height: 22px;
		border-radius: 11px;
		display: grid;
		place-items: center;
		flex: none;
		background: var(--ok-soft);
		color: var(--ok-ink);
	}
	.okmark :global(.ic) {
		width: 14px;
		height: 14px;
		color: inherit;
	}
	.disclosure :global(.chev) {
		width: 16px;
		height: 16px;
		flex: none;
		color: var(--ink-3);
		transition: transform 0.15s;
	}
	.disclosure[aria-expanded='true'] :global(.chev) {
		transform: rotate(180deg);
	}
	@media (prefers-reduced-motion: reduce) {
		.disclosure :global(.chev) {
			transition: none;
		}
	}
	/* Phones: the slim line keeps its label, the assistant link shows its icon only. */
	@media (max-width: 479px) {
		.slim .assist {
			width: 36px;
			padding: 0;
			justify-content: center;
		}
		.slim .assist-label {
			position: absolute;
			width: 1px;
			height: 1px;
			overflow: hidden;
			clip: rect(0 0 0 0);
			white-space: nowrap;
		}
	}
	.tick {
		width: 24px;
		height: 24px;
		border-radius: 12px;
		display: grid;
		place-items: center;
		border: 2px solid var(--line-2);
		flex: none;
		box-sizing: border-box;
	}
	.tick.done {
		background: var(--ok);
		border-color: var(--ok);
		color: #fff;
	}
	.tick :global(.ic) {
		width: 14px;
		height: 14px;
	}
</style>
