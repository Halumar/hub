<script>
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { onMount } from 'svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Tabs from '$lib/ui/Tabs.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import GeneralTab from './components/GeneralTab.svelte';
	import NetworkTab from './components/NetworkTab.svelte';
	import AdvancedTab from './components/AdvancedTab.svelte';
	import SystemTab from './components/SystemTab.svelte';
	import { settings } from './settings-state.svelte.js';

	// The active tab is kept in the URL (?tab=…) so reloads and links keep it.
	const TAB_IDS = ['general', 'network', 'advanced', 'system'];
	let activeTab = $state('general');

	let tabs = $derived([
		{ id: 'general', label: t('settings.tabs.general'), icon: 'gear' },
		{ id: 'network', label: t('settings.tabs.network'), icon: 'wifi' },
		{ id: 'advanced', label: t('settings.tabs.advanced'), icon: 'key' },
		{ id: 'system', label: t('settings.tabs.system'), icon: 'terminal' },
	]);

	let failedLabels = $derived(Object.keys(settings.loadErrors).map((k) => t(`settings.loadLabels.${k}`)));

	function setTab(id) {
		activeTab = id;
		const url = new URL(page.url);
		if (id === 'general') url.searchParams.delete('tab');
		else url.searchParams.set('tab', id);
		try {
			replaceState(url, page.state);
		} catch {
			// Router not ready yet (very early call); the tab still switches.
		}
	}

	onMount(() => {
		const requested = page.url.searchParams.get('tab');
		if (requested && TAB_IDS.includes(requested)) activeTab = requested;
		settings.loadAll();
	});
</script>

<div class="page-narrow">
	<PageHeader title={t('settings.title')} subtitle={t('settings.subtitle')} />

	{#if failedLabels.length > 0}
		<Banner tone="error" title={t('settings.loadErrorTitle')} class="mb-4" testid="settings-load-error">
			{t('settings.loadErrorText', { names: failedLabels.join(', ') })}
			{#snippet actions()}<button type="button" class="btn btn-sm" onclick={() => settings.retryFailed()}>{t('common.retry')}</button>{/snippet}
		</Banner>
	{/if}

	<Tabs items={tabs} value={activeTab} label={t('settings.sections')} onchange={setTab}>
		<div class="stack">
			{#if activeTab === 'general'}
				<GeneralTab />
			{:else if activeTab === 'network'}
				<NetworkTab />
			{:else if activeTab === 'advanced'}
				<AdvancedTab />
			{:else if activeTab === 'system'}
				<SystemTab />
			{/if}
		</div>
	</Tabs>
</div>
