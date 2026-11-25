<script>
	/** "Sync from Home Assistant" dialog: adds new HA-assigned entities to the area. */
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { syncProblems } from '$lib/sync-result.js';
	import { availableDomains, domainIcon } from '$lib/data/domains.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';

	let { open = $bindable(false), area, onReload } = $props();

	let selected = $state(new Set(availableDomains.map((d) => d.id)));
	let syncing = $state(false);
	let error = $state('');

	function toggle(id) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function toggleAll() {
		selected = selected.size === availableDomains.length ? new Set() : new Set(availableDomains.map((d) => d.id));
	}

	async function sync() {
		if (!area?.ha_area_id) return;
		syncing = true;
		error = '';
		try {
			const domains = selected.size < availableDomains.length ? [...selected] : null;
			const result = await api.syncAreaEntities(area.id, domains);
			if (result.entities_added > 0) {
				addToast(t('editor.syncAdded', { count: result.entities_added }), 'success');
				await onReload?.();
			} else {
				addToast(t('editor.syncNone'), 'info');
			}
			const problems = syncProblems(result);
			if (problems) addToast(problems, 'warning', 12000);
			open = false;
		} catch (e) {
			error = t('editor.syncFailed', { message: e.message });
		} finally {
			syncing = false;
		}
	}
</script>

<Modal bind:open title={t('editor.syncTitle')} description={t('editor.syncDescription')} size="md" dismissible={!syncing}>
	<div class="stack">
		{#if !area?.ha_area_id}<Banner tone="warn">{t('editor.syncNotLinked')}</Banner>{/if}
		{#if error}<Banner tone="error">{error}</Banner>{/if}
		<fieldset class="field">
			<legend class="field-label mb-1.5">{t('editor.syncTypes')} · {selected.size}/{availableDomains.length}</legend>
			<div class="row">
				<button type="button" class="chip" onclick={toggleAll}>{selected.size === availableDomains.length ? t('areas.selectNone') : t('areas.selectAll')}</button>
				{#each availableDomains as d (d.id)}
					<button type="button" class="chip" aria-pressed={selected.has(d.id) ? 'true' : 'false'} onclick={() => toggle(d.id)}>
						<Icon name={domainIcon(d.id)} />{t(`areas.domains.${d.id}`)}
					</button>
				{/each}
			</div>
		</fieldset>
	</div>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={syncing}>{t('common.cancel')}</button>
		<button type="button" class="btn btn-primary" onclick={sync} disabled={syncing || !area?.ha_area_id || selected.size === 0}>
			<Icon name="sync" />{syncing ? t('editor.syncing') : t('editor.sync')}
		</button>
	{/snippet}
</Modal>

<style>
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
	}
</style>
