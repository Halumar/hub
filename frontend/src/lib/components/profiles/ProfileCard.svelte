<script>
	/**
	 * Profile card (list): symbol, name (2 lines reserved), badges on one line
	 * (+N), summary sentence (3 lines reserved), devices and adjustments.
	 * All cards of the grid are equally large. The whole card opens
	 * the editor (RowLink).
	 */
	import Icon from '$lib/ui/Icon.svelte';
	import RowLink from '$lib/ui/RowLink.svelte';
	import ProfileIcon from './ProfileIcon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { kindIcon, kindLabel, profileSummary } from '$lib/profiles/format.js';
	import { shownPlatforms } from '$lib/device-info.js';

	let { profile, href, ctx = {} } = $props();

	let badges = $derived.by(() => {
		const b = [];
		b.push({ tone: '', icon: kindIcon(profile.kind || 'person'), text: kindLabel(profile.kind || 'person'), id: 'kind' });
		if (profile.child_protection) b.push({ tone: 'badge-accent', icon: 'child', text: t('profiles.childProtection'), id: 'child' });
		if (profile.suggestion_update) b.push({ tone: 'badge-info', icon: 'sparkle', text: t('profiles.newVersion'), id: 'update' });
		const defaultFor = shownPlatforms(profile.default_for);
		if (defaultFor.length) b.push({ tone: '', icon: 'check', text: t('profiles.defaultFor', { type: defaultFor.map((s) => t(`profiles.slots.${s}`)).join(', ') }), id: 'default' });
		return b;
	});
	let shown = $derived(badges.slice(0, 3));
	let more = $derived(badges.length - shown.length);
</script>

<article class="card pcard rowlink" aria-labelledby="pc-{profile.id}" data-testid="profile-card" data-eq="profiles">
	<div class="ph">
		<ProfileIcon icon={profile.icon} color={profile.color} />
		<h3 id="pc-{profile.id}" class="t-card clamp2"><RowLink {href} testid="profile-open">{profile.name}</RowLink></h3>
		<span class="rowlink-chev" aria-hidden="true"><Icon name="chev" /></span>
	</div>
	<div class="badges" aria-label={badges.map((b) => b.text).join(', ')}>
		{#each shown as b (b.id)}<span class="badge {b.tone}" data-testid="profile-badge-{b.id}"><Icon name={b.icon} /><span class="truncate">{b.text}</span></span>{/each}
		{#if more > 0}<span class="badge" title={badges.slice(2).map((b) => b.text).join(', ')}>+{more}</span>{/if}
	</div>
	<p class="t-help clamp3 sum">{profile.description || profileSummary(profile, ctx)}</p>
	<div class="foot">
		<span class="inline-flex items-center gap-1"><Icon name="iphone" size="14" />{t('devices.count', { count: profile.device_count || 0 })}</span>
		{#if profile.adjusted_device_count}
			<span class="badge badge-warn" data-testid="profile-adjusted"><Icon name="sliders" />{t('profiles.adjustedDevices', { count: profile.adjusted_device_count })}</span>
		{:else}
			<span class="t-muted">{t('profiles.noAdjustments')}</span>
		{/if}
	</div>
</article>

<style>
	.pcard {
		display: grid;
		grid-template-rows: auto auto 1fr auto;
		gap: 10px;
		height: 100%;
		box-sizing: border-box;
		min-width: 0;
	}
	.ph {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 10px;
		align-items: start;
	}
	.ph h3 {
		margin: 0;
		align-self: center;
	}
	.clamp2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(2 * 1.35em);
		line-height: 1.35;
	}
	.clamp3 {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(3 * 1.45em);
		line-height: 1.45;
		margin: 0;
	}
	/* Badges: at most two lines, the place is reserved. */
	.badges {
		display: flex;
		gap: 4px 6px;
		flex-wrap: wrap;
		overflow: hidden;
		height: 48px;
		align-content: flex-start;
		min-width: 0;
	}
	.badges .badge {
		min-width: 0;
		max-width: 100%;
	}
	.foot {
		display: flex;
		flex-wrap: wrap;
		gap: 6px 10px;
		align-items: center;
		font-size: var(--fs-sm);
		color: var(--ink-2);
		border-top: 1px solid var(--line);
		padding-top: 10px;
	}
</style>
