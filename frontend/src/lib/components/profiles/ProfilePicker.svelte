<script>
	/**
	 * Profile choice as equal tiles: native radio group (arrow keys),
	 * symbol, name (2 lines reserved) and one hint line. `disabledReason(p)`
	 * returns a text when a profile cannot be chosen (e.g. parental controls
	 * for guests). An optional "auto" tile stands for "default of the device
	 * type" (value null).
	 */
	import ProfileIcon from './ProfileIcon.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let { profiles = [], value = $bindable(null), label, auto = null, disabledReason = null, onchange = null, name = 'profile', hint = null, testid = 'profile-picker' } = $props();

	function pick(id) {
		value = id;
		onchange?.(id);
	}

	function lineOf(p) {
		if (hint) return hint(p);
		if (p.child_protection) return t('profiles.childProtection');
		return p.rules?.remote_access ? t('profiles.sum.away') : t('profiles.sum.homeOnly');
	}
</script>

<div class="tiles" role="radiogroup" aria-label={label} data-testid={testid} data-eq="profile-tiles">
	{#if auto}
		<label class="tile" class:on={value === null}>
			<input type="radio" {name} checked={value === null} onchange={() => pick(null)} data-testid="{testid}-auto" />
			<span class="ic"><Icon name="sparkle" /></span>
			<span class="nm clamp2">{auto.label}</span>
			<small class="t-help clamp2">{auto.hint}</small>
		</label>
	{/if}
	{#each profiles as p (p.id)}
		{@const reason = disabledReason?.(p) || ''}
		<label class="tile" class:on={value === p.id} class:off={!!reason} title={reason || undefined}>
			<input type="radio" {name} checked={value === p.id} disabled={!!reason} onchange={() => pick(p.id)} data-testid="{testid}-{p.id}" />
			<ProfileIcon icon={p.icon} color={p.color} size={30} />
			<span class="nm clamp2">{p.name}</span>
			<small class="t-help clamp2">{reason || lineOf(p)}</small>
		</label>
	{/each}
</div>

<style>
	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
		grid-auto-rows: 1fr;
		gap: 8px;
	}
	.tile {
		position: relative;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		grid-template-rows: auto 1fr;
		align-items: center;
		gap: 4px 10px;
		padding: 10px 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		cursor: pointer;
		min-width: 0;
	}
	.tile input {
		position: absolute;
		opacity: 0;
		inset: 0;
		margin: 0;
		cursor: pointer;
	}
	.tile:focus-within {
		outline: 2px solid var(--focus);
		outline-offset: 1px;
	}
	.tile.on {
		border-color: var(--ink);
		box-shadow: 0 0 0 1px var(--ink);
	}
	.tile.off {
		cursor: not-allowed;
		background: var(--surface-2);
	}
	.tile.off input {
		cursor: not-allowed;
	}
	.ic {
		width: 30px;
		height: 30px;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--fill-2);
	}
	.nm {
		font-weight: 650;
		min-width: 0;
	}
	.tile small {
		grid-column: 1 / -1;
		align-self: start;
	}
	.clamp2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(2 * 1.35em);
		line-height: 1.35;
		min-width: 0;
	}
</style>
