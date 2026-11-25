<script>
	/**
	 * What remote access means on "Hub only": the device gets a read-only
	 * Home Assistant key and can read every state in Home Assistant. Shown
	 * wherever remote access is switched on while the path is "Hub only"
	 * (profile editor, device adjustment, pairing, change preview), always
	 * visible next to the control. variant: 'profile' | 'device'. `from`: the
	 * end of a temporary adjustment after which the device is on "Hub only"
	 * with remote access (while it is not before).
	 */
	import Banner from '$lib/ui/Banner.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { formatWeekdayTime } from '$lib/utils.js';

	let { variant = 'device', from = null, testid = 'remote-key-warning', class: cls = '' } = $props();
</script>

<Banner tone="warn" icon="key" class={cls} {testid}>
	{#if from}<b data-testid="{testid}-from" data-at={typeof from === 'string' ? from : from.toISOString()}>{t('profiles.editor.remoteKey.from', { date: formatWeekdayTime(from) })}</b>{' '}{/if}
	{t(`profiles.editor.remoteKey.${variant === 'profile' ? 'profile' : 'device'}`)}
</Banner>
