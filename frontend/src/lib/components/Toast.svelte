<script>
	/** Toast stack (bottom left, outside the action areas), announced politely. */
	import { toasts, dismissToast } from '$lib/stores/toast.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	const ICON = { success: 'checkcircle', warning: 'warn', error: 'error', info: 'info' };
</script>

<div class="toasts" role="status" aria-live="polite" aria-relevant="additions">
	{#each toasts.list as toast (toast.id)}
		<div class="toast toast-{toast.type}" data-testid="toast" data-type={toast.type}>
			<Icon name={ICON[toast.type] || 'info'} />
			<span class="flex-1 leading-snug">{toast.message}</span>
			<button type="button" class="toast-close" onclick={() => dismissToast(toast.id)} aria-label={t('common.dismiss')}>
				<Icon name="x" />
			</button>
		</div>
	{/each}
</div>
