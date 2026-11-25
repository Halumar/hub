<script>
	/**
	 * CSV export of the log: current filters or everything stored,
	 * optionally without people's names; the warning that the log leaves the
	 * hub. Hidden people are never exported (the hub does not return them).
	 */
	import { t } from '$lib/i18n/index.svelte.js';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import { csv, dayKey } from '$lib/log/describe.js';
	import { logState, queryOf } from '$lib/log/state.svelte.js';

	let { open = $bindable(false), total = 0, hidden = [] } = $props();

	let scope = $state('filter');
	let anon = $state(false);
	let busy = $state(false);

	async function download() {
		busy = true;
		try {
			const params = scope === 'filter' ? queryOf(logState.filters) : {};
			const { entries } = await api.exportLog(params);
			const blob = new Blob([csv(entries, anon)], { type: 'text/csv;charset=utf-8' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${t('log.csvFile')}-${dayKey(new Date().toISOString())}.csv`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			setTimeout(() => URL.revokeObjectURL(url), 1000);
			addToast(t('log.exported', { count: entries.length }), 'success');
			open = false;
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:open title={t('log.exportTitle')} description={t('log.exportDescription')} testid="log-export">
	<div class="stack">
		<div class="field">
			<span class="field-label" id="ex-which">{t('log.exportWhich')}</span>
			<div class="seg even full" role="group" aria-labelledby="ex-which" data-eq="exscope">
				<button type="button" aria-pressed={scope === 'filter'} onclick={() => (scope = 'filter')}>{t('log.exportFiltered')}</button>
				<button type="button" aria-pressed={scope === 'all'} onclick={() => (scope = 'all')}>{t('log.exportAll', { count: total })}</button>
			</div>
		</div>
		<label class="check box">
			<input type="checkbox" bind:checked={anon} data-testid="log-export-anon" />
			<span><b>{t('log.exportAnon')}</b><br /><span class="t-help">{t('log.exportAnonHelp')}</span></span>
		</label>
		<div class="banner banner-warn"><Icon name="warn" /><div class="banner-body">{t('log.exportWarn')}</div></div>
		{#if hidden.length}<p class="t-help">{t('log.exportHidden', { names: hidden.join(', ') })}</p>{/if}
	</div>
	{#snippet footer()}
		<div class="foot-eq" data-eq="exfoot">
			<button type="button" class="btn" onclick={() => (open = false)}>{t('lognotify.cancel')}</button>
			<button type="button" class="btn btn-primary" onclick={download} disabled={busy} data-testid="log-export-download"><Icon name="download" />{t('log.exportDownload')}</button>
		</div>
	{/snippet}
</Modal>
