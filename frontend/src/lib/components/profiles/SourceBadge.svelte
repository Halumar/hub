<script>
	/**
	 * Origin of an effective rule value on a device (reason codes):
	 * symbol + text, never color alone. A "Hub only" boundary gets a
	 * "Why?" toggle with the boundary's full catalog sentence.
	 */
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L, boundaryInfo, reasonText } from '$lib/profiles/catalog.svelte.js';
	import { formatValue, kindLabel, untilText } from '$lib/profiles/format.js';
	import { platformLabel } from '$lib/device-info.js';

	let { entry, rule, profileName = '', platform = '', ctx = {}, testid = undefined } = $props();

	let open = $state(false);
	const uid = `sb${Math.random().toString(36).slice(2, 8)}`;

	let info = $derived.by(() => {
		const e = entry || {};
		const key = rule?.key;
		const fmt = (v) => formatValue(key, v, ctx);
		switch (e.reason) {
			case 'catalog_default':
				return { icon: 'info', tone: '', text: reasonText('catalog_default') || t('profiles.src.default') };
			case 'profile':
				return e.fixed ? { icon: 'lock', tone: 'fixed', text: t('profiles.src.fixed') } : { icon: 'template', tone: '', text: t('profiles.src.profile') };
			case 'user_choice':
				return { icon: 'user', tone: '', text: t('profiles.src.userChoice', { value: fmt(e.value) }) };
			case 'device':
				return { icon: 'sliders', tone: 'warn', text: t('profiles.src.device', { value: fmt(e.profile_value) }) };
			case 'device_temporary':
				return { icon: 'clock', tone: 'warn', text: t('profiles.src.temporary', { until: untilText(e.expires_at), then: fmt(e.then) }) };
			case 'boundary_guest':
				return { icon: 'users', tone: 'info', text: reasonText('boundary_guest') };
			case 'boundary_child_protection':
				return { icon: 'child', tone: 'info', text: reasonText('boundary_child_protection') };
			case 'limited_by_phone':
				// A watch never has more than its phone.
				return { icon: 'iphone', tone: 'info', text: reasonText('limited_by_phone', { device: ctx.phoneName || '–' }) };
			case 'boundary_hub_only':
				return { icon: 'key', tone: 'info', text: reasonText('boundary_hub_only'), why: L(boundaryInfo('hub_only')?.effects) };
			case 'platform_needs_key':
				return { icon: 'key', tone: 'info', text: reasonText('platform_needs_key', { platform: platformLabel(platform) || platform }) };
			case 'prerequisite_missing':
				return { icon: 'key', tone: 'warn', text: reasonText('prerequisite_missing') };
			case 'not_applicable':
				return { icon: 'minus', tone: 'na', text: reasonText('not_applicable', { platform: platformLabel(platform) || platform }) };
			case 'requires_remote_access':
			case 'requires_location':
			case 'requires_child_protection':
				return { icon: 'minus', tone: 'na', text: reasonText(e.reason) };
			case 'requires_profile_kind':
				return { icon: 'minus', tone: 'na', text: reasonText(e.reason, { kinds: (rule?.profile_kinds || ['person']).map((k) => kindLabel(k)).join(', ') }) };
			case 'access_expired':
				return { icon: 'error', tone: 'err', text: reasonText('access_expired') };
			default:
				return { icon: 'info', tone: '', text: reasonText(e.reason) || t('profiles.src.unknown') };
		}
	});
</script>

<span class="sb" data-testid={testid} data-reason={entry?.reason}>
	<span class="badge sbadge {info.tone}"><Icon name={info.icon} /><span class="txt">{info.text}</span></span>
	{#if info.why}
		<button type="button" class="link why" aria-expanded={open ? 'true' : 'false'} aria-controls={uid} onclick={() => (open = !open)}>{t('profiles.why')}</button>
		{#if open}<span id={uid} class="t-help block mt-1">{info.why}</span>{/if}
	{/if}
	{#if entry?.expires_at && entry.reason !== 'device_temporary'}
		<!-- A value that follows a temporary adjustment of another rule (widgets and watch with a temporary remote access). -->
		<span class="t-help" data-testid={testid ? `${testid}-until` : undefined}>{t('profiles.preview.until', { time: untilText(entry.expires_at), then: formatValue(rule?.key, entry.then, ctx) })}</span>
	{/if}
	{#if entry?.reason === 'device' && profileName}<span class="sr-only">{t('profiles.src.profileNamed', { name: profileName })}</span>{/if}
</span>

<style>
	.sb {
		display: inline-flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px 8px;
		min-width: 0;
	}
	.sbadge {
		white-space: normal;
		max-width: 100%;
		align-items: flex-start;
	}
	.sbadge :global(.ic) {
		margin-top: 2px;
		flex: none;
	}
	.txt {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.sbadge.fixed {
		background: var(--accent-soft);
		color: var(--accent-ink);
	}
	.sbadge.warn {
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.sbadge.info {
		background: var(--info-soft);
		color: var(--info-ink);
	}
	.sbadge.err {
		background: var(--err-soft);
		color: var(--err-ink);
	}
	.sbadge.na {
		background: transparent;
		border: 1px dashed var(--line-2);
		color: var(--ink-2);
	}
	.why {
		font-size: var(--fs-cap);
		font-weight: 650;
		background: none;
		border: 0;
		padding: 0;
		min-height: 24px;
		cursor: pointer;
	}
</style>
