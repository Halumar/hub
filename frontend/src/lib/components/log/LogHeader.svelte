<script>
	/**
	 * Head of the log tab: title, alarm/integration status and the
	 * three sections Entries · Notifications · Settings (links, equal width).
	 */
	import { base } from '$app/paths';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import './log.css';

	let { active = 'list', summary = null } = $props();

	const TABS = [
		{ id: 'list', path: '/log', icon: 'list', key: 'log.tabEntries' },
		{ id: 'notify', path: '/log/notify', icon: 'bell', key: 'log.tabNotify' },
		{ id: 'settings', path: '/log/settings', icon: 'gear', key: 'log.tabSettings' },
	];
	let unread = $derived(summary?.unread_alarms || 0);
</script>

<header class="page-head log-head">
	<div class="grow">
		<h1 class="t-page">{t('log.title')}</h1>
		<p class="t-sub">{t('log.subtitle')}</p>
	</div>
	{#if summary}
		<div class="row head-badges">
			{#if unread}
				<span class="badge badge-warn" data-testid="log-unread"><Icon name="warn" />{t('log.newAlarms', { count: unread })}</span>
			{:else}
				<span class="badge badge-ok"><Icon name="check" />{t('log.noNewAlarms')}</span>
			{/if}
			{#if summary.integration_connected}
				<span class="badge badge-ok"><span class="dot"></span>{t('log.integrationOn')}</span>
			{:else}
				<span class="badge badge-warn"><Icon name="wifioff" />{t('log.integrationOff')}</span>
			{/if}
		</div>
	{/if}
</header>
<nav class="seg even logtabs" aria-label={t('log.tabs')} data-eq="logtabs">
	{#each TABS as tab (tab.id)}
		<a href="{base}{tab.path}" aria-current={active === tab.id ? 'page' : undefined} data-testid="logtab-{tab.id}">
			<Icon name={tab.icon} /><span class="lbl">{t(tab.key)}</span>
			{#if tab.id === 'list' && unread}<span class="cnt" aria-hidden="true">{unread}</span>{/if}
		</a>
	{/each}
</nav>

<style>
	.head-badges {
		flex-wrap: wrap;
		gap: 6px;
	}
	.logtabs {
		width: min(100%, 560px);
		margin-bottom: var(--s4);
	}
	.logtabs a {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-height: 36px;
		padding: 0 10px;
		border-radius: var(--r-sm);
		color: var(--ink-2);
		font-size: 13px;
		font-weight: 650;
		text-decoration: none;
		min-width: 0;
		overflow: hidden;
	}
	.logtabs a :global(.ic) {
		width: 16px;
		height: 16px;
		flex: none;
	}
	.logtabs .lbl {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.logtabs a[aria-current='page'] {
		background: var(--surface);
		color: var(--ink);
		box-shadow: 0 1px 3px rgb(0 0 0 / 0.12);
	}
	.logtabs a:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}
	.cnt {
		font-size: 11px;
		font-weight: 700;
		background: var(--warn-soft);
		color: var(--warn-ink);
		border-radius: 8px;
		padding: 1px 6px;
		flex: none;
	}
	@media (max-width: 639px) {
		.logtabs {
			width: 100%;
		}
		.logtabs a {
			min-height: 44px;
			padding: 0 4px;
		}
	}
</style>
