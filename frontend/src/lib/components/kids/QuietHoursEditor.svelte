<script>
	/**
	 * Bedtime windows (kids mode): weekday buttons (always equal width), start/end time, hint when a window runs into the next morning.
	 * `windows` is bindable; [] = no bedtime on this level.
	 */
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { DAYS, DEFAULT_WINDOW, clone, crossesMidnight, daysLabel } from '$lib/data/kids.js';

	let { windows = $bindable(), idPrefix = 'qh', max = 14, onchange = null, noneText = '' } = $props();

	function changed() {
		windows = [...(windows || [])];
		onchange?.(windows);
	}

	function toggleDay(i, day) {
		const w = windows[i];
		w.days = w.days.includes(day) ? w.days.filter((d) => d !== day) : DAYS.filter((d) => d === day || w.days.includes(d));
		changed();
	}

	function add() {
		windows = [...(windows || []), clone(DEFAULT_WINDOW)];
		onchange?.(windows);
	}

	function remove(i) {
		windows = (windows || []).filter((_, j) => j !== i);
		onchange?.(windows);
	}
</script>

<div class="qh" data-testid="quiet-hours-editor">
	{#if !windows?.length}
		<p class="t-help" data-testid="quiet-none">{noneText || t('kids.quiet.noneHint')}</p>
	{/if}
	{#each windows || [] as w, i (i)}
		<div class="win box" data-testid="quiet-window">
			<div class="days" role="group" aria-label={t('kids.quiet.daysOf', { n: i + 1 })}>
				{#each DAYS as day (day)}
					<button
						type="button"
						class="chip day"
						aria-pressed={w.days.includes(day) ? 'true' : 'false'}
						aria-label={t(`kids.days.long.${day}`)}
						title={t(`kids.days.long.${day}`)}
						onclick={() => toggleDay(i, day)}
						data-testid="day-{day}">{t(`kids.days.short.${day}`)}</button
					>
				{/each}
			</div>
			<div class="times">
				<label class="tfield">
					<span class="t-label">{t('kids.quiet.from')}</span>
					<input id="{idPrefix}-start-{i}" type="time" class="input" bind:value={w.start} oninput={changed} required data-testid="quiet-start" />
				</label>
				<label class="tfield">
					<span class="t-label">{t('kids.quiet.to')}</span>
					<input id="{idPrefix}-end-{i}" type="time" class="input" bind:value={w.end} oninput={changed} required data-testid="quiet-end" />
				</label>
				<button type="button" class="btn btn-ghost btn-icon rm" onclick={() => remove(i)} aria-label={t('kids.quiet.remove', { n: i + 1 })} data-testid="quiet-remove"><Icon name="trash" /></button>
			</div>
			<p class="t-help">
				{#if !w.days.length}
					<span class="t-err">{t('kids.quiet.pickDay')}</span>
				{:else if w.start === w.end}
					{t('kids.quiet.wholeDayHint', { days: daysLabel(w.days) })}
				{:else if crossesMidnight(w)}
					{t('kids.quiet.overnight', { days: daysLabel(w.days), start: w.start, end: w.end })}
				{:else}
					{t('kids.quiet.sameDay', { days: daysLabel(w.days), start: w.start, end: w.end })}
				{/if}
			</p>
		</div>
	{/each}
	{#if (windows?.length || 0) < max}
		<div><button type="button" class="btn btn-sm" onclick={add} data-testid="quiet-add"><Icon name="plus" />{t('kids.quiet.add')}</button></div>
	{/if}
</div>

<style>
	.qh {
		display: grid;
		gap: 10px;
		min-width: 0;
	}
	.win {
		display: grid;
		gap: 10px;
		min-width: 0;
	}
	/* Equal sizes: seven equal buttons, whatever the language. */
	.days {
		display: grid;
		grid-template-columns: repeat(7, minmax(0, 1fr));
		gap: 6px;
	}
	.day {
		justify-content: center;
		min-width: 0;
		min-height: var(--h-ctl);
		padding-inline: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.times {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
		gap: 10px;
		align-items: end;
	}
	.tfield {
		display: grid;
		gap: 4px;
		min-width: 0;
	}
	.tfield .input {
		width: 100%;
		min-width: 0;
	}
</style>
