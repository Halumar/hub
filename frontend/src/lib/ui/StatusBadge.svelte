<script>
	/** Online/offline as text + dot (never color alone). */
	import { t } from '$lib/i18n/index.svelte.js';
	let { online = false, label = '' } = $props();
</script>

<span class="badge {online ? 'badge-ok' : ''}"><span class="dot" aria-hidden="true"></span>{label || (online ? t('common.online') : t('common.offline'))}</span>
