<script>
	/**
	 * Live preview of tiles in the apps (approximation of HubCard):
	 * iPhone = the tile's section in the 2-column grid between its neighbours;
	 * Apple TV = one row of up to 4 tiles with the edited tile focused (lift).
	 * Style A "calm" tints only the icon chip of active tiles, style B also the
	 * card. Purely visual (aria-hidden); the panel adds a text summary.
	 *
	 * Props: cards (cardModel[] from $lib/data/tiles.js), editId, dev
	 * ('phone'|'tv'), style ('calm'|'color'), title, tvHints [{key, text}].
	 */
	import { t } from '$lib/i18n/index.svelte.js';

	let { cards = [], editId = null, dev = 'phone', style = 'calm', title = '', subtitle = '', tvHints = [], large = false } = $props();

	// TV: a row of four with the edited tile; hidden tiles only as placeholder of the edited one.
	let tvCards = $derived.by(() => {
		const visible = cards.filter((c) => !c.hidden || c.entityId === editId);
		const i = Math.max(0, visible.findIndex((c) => c.entityId === editId));
		const start = Math.max(0, Math.min(i - 1, visible.length - 4));
		return visible.slice(start, start + 4);
	});
	let phoneCards = $derived(cards.filter((c) => !c.hidden || c.entityId === editId));
	let phoneEl = $state(null);
	let tvWidth = $state(370);
	let tvScale = $derived(Math.min(0.4, tvWidth / 960));

	// Scroll the scaled phone so the edited card is visible.
	$effect(() => {
		void phoneCards;
		void editId;
		if (!phoneEl || dev !== 'phone') return;
		const card = phoneEl.querySelector('.card.edit');
		const scale = large ? 0.8 : 0.555;
		phoneEl.style.top = card ? `${-Math.max(0, card.offsetTop * scale - 70)}px` : '0px';
	});
</script>

{#snippet card(c, opts)}
	{#if c.hidden}
		<div class="card hiddenhere" class:w2={c.wide && dev === 'phone'} class:edit={opts.edit}>
			<span>{c.name}</span><span>{t('tiles.hiddenHere')}</span>
		</div>
	{:else}
		<div
			class="card"
			class:w2={c.wide && dev === 'phone'}
			class:h2={c.tall}
			class:edit={opts.edit && dev === 'phone'}
			class:focus={opts.focus}
			class:action={c.variant === 'action'}
			data-t={c.tint || undefined}
		>
			{#if c.variant === 'action'}
				<span class="chipic"><svg viewBox="0 0 24 24"><path fill="currentColor" d={c.path} /></svg></span>
				<div class="nm">{c.name}</div>
			{:else if c.variant === 'large'}
				<div class="top"><span class="chipic"><svg viewBox="0 0 24 24"><path fill="currentColor" d={c.path} /></svg></span></div>
				<div class="bigv">{c.bigValue}</div>
				<div class="nm">{c.name}</div>
				<div class="st">{c.status}</div>
			{:else}
				<div class="top">
					<span class="chipic"><svg viewBox="0 0 24 24"><path fill="currentColor" d={c.path} /></svg></span>
					{#if c.variant === 'slider' && dev === 'tv'}<div class="level"><b style:width="{c.level}%"></b></div>{/if}
					{#if c.value}<span class="val">{c.value}</span>{/if}
				</div>
				<div class="nm">{c.name}</div>
				<div class="st">{c.status}</div>
				{#if c.secondary}<div class="st2">{c.secondary}</div>{/if}
				{#if c.variant === 'slider' && dev === 'phone'}
					<div class="slider"><b style:width="{c.level}%"></b><span>{c.on ? `${c.level} %` : t('tiles.stateOff')}</span></div>
				{/if}
			{/if}
		</div>
	{/if}
{/snippet}

<div class="pvroot style-{style}" class:large aria-hidden="true">
	{#if dev === 'phone'}
		<div class="mini-phone">
			<div class="clip">
				<div class="app phone" bind:this={phoneEl}>
					<div class="sb"><span>9:41</span><span class="sbi">▮▮▮</span></div>
					<h1>{title}</h1>
					{#if subtitle}<div class="sub">{subtitle}</div>{/if}
					<div class="cgrid">
						{#each phoneCards as c (c.entityId)}
							{@render card(c, { edit: c.entityId === editId })}
						{/each}
					</div>
				</div>
			</div>
		</div>
	{:else}
		<div class="mini-tv">
			<div class="clip" bind:clientWidth={tvWidth}>
				<div class="app tv" style:transform="scale({tvScale})">
					<div class="safe">
						<div class="head"><h1>{title}</h1><span class="pill"><i></i>{t('tiles.connected')}</span></div>
						<div class="tgrid">
							{#each tvCards as c (c.entityId)}
								{@render card(c, { focus: c.entityId === editId, edit: c.entityId === editId })}
							{/each}
						</div>
						<div class="hints">
							{#each tvHints as h (h.key)}<span><b>{h.key}</b>{h.text}</span>{/each}
						</div>
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.pvroot {
		--light: #f5b82e;
		--light-on: #3b2a00;
		--heat: #f06a2c;
		--heat-soft: #ffe8dc;
		--cool: #2f8ceb;
		--cool-soft: #e3f0ff;
		--sea: #1f9db0;
		--sea-ink: #0b6e7a;
		--sea-soft: #ddf4f6;
		--media: #7a5af0;
		--media-soft: #eee9ff;
		--safe: #2f9e55;
		--safe-soft: #e3f5e9;
		--card-shadow: 0 1px 2px rgb(0 0 0 / 0.06), 0 2px 8px rgb(0 0 0 / 0.05);
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		justify-items: center;
		padding: 8px 0;
	}
	:global([data-theme='dark']) .pvroot {
		--heat-soft: #3d2116;
		--cool-soft: #14283f;
		--sea-ink: #6fd3e0;
		--sea-soft: #0f2e33;
		--media-soft: #241b45;
		--safe-soft: #14301f;
		--card-shadow: none;
	}
	.mini-phone {
		width: 228px;
		height: 300px;
		border-radius: 30px;
		background: #0b0b0e;
		padding: 6px;
		overflow: hidden;
	}
	.mini-phone .clip {
		width: 100%;
		height: 100%;
		border-radius: 25px;
		overflow: hidden;
		position: relative;
		background: var(--bg);
	}
	.mini-phone .app {
		transform: scale(0.555);
		transform-origin: 0 0;
		width: 390px;
		position: absolute;
		left: 0;
		top: 0;
		transition: top 0.2s;
	}
	.large .mini-phone {
		width: 324px;
		height: 640px;
		border-radius: 42px;
		padding: 8px;
	}
	.large .mini-phone .clip {
		border-radius: 35px;
	}
	.large .mini-phone .app {
		transform: scale(0.8);
	}
	.mini-tv {
		width: 100%;
		max-width: 380px;
		aspect-ratio: 380 / 226;
		background: #050506;
		border-radius: 8px;
		padding: 5px;
		box-sizing: border-box;
	}
	.mini-tv .clip {
		width: 100%;
		height: 100%;
		overflow: hidden;
		border-radius: 4px;
		position: relative;
	}
	.mini-tv .app {
		transform-origin: 0 0;
		width: 960px;
		height: 560px;
		position: absolute;
		left: 0;
		top: 0;
	}
	.app {
		color: var(--ink);
		background: var(--bg);
		line-height: 1.25;
		font-family: var(--font);
	}
	.app.phone {
		padding: 0 16px 30px;
		min-height: 760px;
	}
	.sb {
		height: 50px;
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 4px 18px 0 24px;
		font-weight: 650;
		font-size: 16px;
	}
	.sbi {
		letter-spacing: -2px;
	}
	.app.phone h1 {
		font-size: 30px;
		letter-spacing: -0.02em;
		margin: 10px 4px 2px;
	}
	.sub {
		font-size: 14px;
		color: var(--ink-2);
		margin: 0 4px 12px;
	}
	.cgrid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
		grid-auto-flow: row dense;
		margin-top: 12px;
	}
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 3px;
		min-height: 122px;
		padding: 13px;
		border-radius: 22px;
		background: var(--surface);
		box-shadow: var(--card-shadow);
		overflow: hidden;
		box-sizing: border-box;
	}
	.card.w2 {
		grid-column: span 2;
	}
	.card.h2 {
		grid-row: span 2;
	}
	.top {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 8px;
		margin-bottom: auto;
	}
	.chipic {
		width: 38px;
		height: 38px;
		border-radius: 11px;
		display: grid;
		place-items: center;
		background: var(--fill-2);
		color: var(--ink-2);
		flex: none;
	}
	.chipic svg {
		width: 21px;
		height: 21px;
	}
	.val {
		font-size: 14px;
		font-weight: 650;
		color: var(--ink-2);
	}
	.nm {
		font-size: 16px;
		font-weight: 650;
		line-height: 1.2;
		margin-top: 8px;
		overflow-wrap: anywhere;
	}
	.st {
		font-size: 14px;
		color: var(--ink-2);
	}
	.st2 {
		font-size: 13px;
		color: var(--ink-3);
	}
	.bigv {
		font-size: 30px;
		font-weight: 700;
		letter-spacing: -0.02em;
		margin-top: 8px;
	}
	.slider {
		margin-top: 10px;
		height: 36px;
		border-radius: 12px;
		background: var(--fill-2);
		position: relative;
		overflow: hidden;
	}
	.slider b {
		position: absolute;
		inset: 0 auto 0 0;
		border-radius: 12px;
		background: var(--tfill, var(--fill-3));
	}
	.slider span {
		position: absolute;
		left: 12px;
		top: 50%;
		transform: translateY(-50%);
		font-size: 13px;
		font-weight: 700;
		color: var(--ink);
	}
	.level {
		flex: 1;
		align-self: center;
		margin: 0 4px 0 6px;
		height: 10px;
		border-radius: 5px;
		background: var(--fill-2);
		overflow: hidden;
	}
	.level b {
		display: block;
		height: 100%;
		background: var(--tfill, var(--ink-3));
		border-radius: 5px;
	}
	.card.action {
		align-items: center;
		justify-content: center;
		text-align: center;
		gap: 8px;
	}
	.card.action .chipic {
		width: 52px;
		height: 52px;
		border-radius: 16px;
	}
	.card.action .chipic svg {
		width: 28px;
		height: 28px;
	}
	.card.action .nm {
		margin-top: 0;
	}
	.card.hiddenhere {
		background: none;
		box-shadow: none;
		border: 2px dashed var(--line-2);
		align-items: center;
		justify-content: center;
		color: var(--ink-3);
		font-size: 13px;
		font-weight: 600;
		text-align: center;
	}
	.card.edit {
		outline: 3px solid var(--accent);
		outline-offset: 2px;
	}
	/* tints (HubTint families; only active cards carry data-t) */
	.card[data-t] .chipic {
		color: #fff;
	}
	.card[data-t='light'] {
		--tfill: var(--light);
	}
	.card[data-t='light'] .chipic {
		background: var(--light);
		color: var(--light-on);
	}
	.card[data-t='heat'] {
		--tfill: var(--heat);
	}
	.card[data-t='heat'] .chipic {
		background: var(--heat);
	}
	.card[data-t='cool'] {
		--tfill: var(--cool);
	}
	.card[data-t='cool'] .chipic {
		background: var(--cool);
	}
	.card[data-t='sea'] {
		--tfill: var(--sea);
	}
	.card[data-t='sea'] .chipic {
		background: var(--sea);
	}
	.card[data-t='media'] {
		--tfill: var(--media);
	}
	.card[data-t='media'] .chipic {
		background: var(--media);
	}
	.card[data-t='safe'] {
		--tfill: var(--safe);
	}
	.card[data-t='safe'] .chipic {
		background: var(--safe);
	}
	.card[data-t='accent'] {
		--tfill: var(--accent);
	}
	.card[data-t='accent'] .chipic {
		background: var(--accent);
	}
	.card[data-t='soft-sea'] .chipic {
		background: var(--sea-soft);
		color: var(--sea-ink);
	}
	.card[data-t='light'] .slider span {
		color: var(--light-on);
	}
	.card[data-t]:not([data-t='light']):not([data-t='soft-sea']) .slider span {
		color: #fff;
	}
	.style-color .card[data-t='light'] {
		background: var(--light-soft);
	}
	.style-color .card[data-t='heat'] {
		background: var(--heat-soft);
	}
	.style-color .card[data-t='cool'] {
		background: var(--cool-soft);
	}
	.style-color .card[data-t='sea'] {
		background: var(--sea-soft);
	}
	.style-color .card[data-t='media'] {
		background: var(--media-soft);
	}
	.style-color .card[data-t='safe'] {
		background: var(--safe-soft);
	}
	.style-color .card[data-t='accent'] {
		background: var(--accent-soft);
	}
	.style-color .card:not([data-t]):not(.hiddenhere) {
		background: var(--fill);
		box-shadow: none;
	}
	.style-color .card[data-t] .slider {
		background: rgb(255 255 255 / 0.55);
	}
	:global([data-theme='dark']) .style-color .card[data-t] .slider {
		background: rgb(0 0 0 / 0.25);
	}
	/* Apple TV */
	.app.tv {
		position: relative;
		background:
			radial-gradient(600px 350px at 15% -10%, #fff6ea 0%, transparent 60%),
			radial-gradient(450px 300px at 100% 110%, #e3eef6 0%, transparent 60%),
			#ece9e4;
	}
	:global([data-theme='dark']) .app.tv {
		background: radial-gradient(600px 350px at 15% -10%, #2a2016 0%, transparent 60%), #121316;
	}
	.safe {
		position: absolute;
		inset: 30px 40px;
	}
	.head {
		display: flex;
		align-items: center;
		gap: 12px;
		height: 48px;
	}
	.head h1 {
		font-size: 30px;
		letter-spacing: -0.02em;
		margin: 0;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 28px;
		padding: 0 12px;
		border-radius: 14px;
		background: var(--surface);
		font-size: 12px;
		font-weight: 650;
	}
	.pill i {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: #34c759;
	}
	.tgrid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		grid-auto-rows: 150px;
		gap: 18px;
		margin-top: 60px;
	}
	.app.tv .card {
		min-height: 0;
		padding: 13px 16px;
		border-radius: 16px;
		box-shadow: none;
		background: rgb(255 255 255 / 0.62);
		transition: transform 0.2s;
	}
	:global([data-theme='dark']) .app.tv .card:not(.hiddenhere) {
		background: rgb(255 255 255 / 0.08);
	}
	.app.tv .card.hiddenhere {
		background: none;
	}
	.app.tv .card .chipic {
		width: 30px;
		height: 30px;
		border-radius: 9px;
	}
	.app.tv .card .chipic svg {
		width: 18px;
		height: 18px;
	}
	.app.tv .card.action .chipic {
		width: 48px;
		height: 48px;
	}
	.app.tv .card .nm {
		font-size: 17px;
		font-weight: 700;
	}
	.app.tv .card .st {
		font-size: 13px;
	}
	.app.tv .card.focus {
		transform: scale(1.08);
		background: #fff;
		color: #121317;
		box-shadow: 0 18px 40px rgb(0 0 0 / 0.22);
		z-index: 2;
	}
	:global([data-theme='dark']) .app.tv .card.focus {
		background: #fff;
	}
	.app.tv .card.focus .st,
	.app.tv .card.focus .st2 {
		color: #4a4e59;
	}
	.style-color .app.tv .card[data-t='light']:not(.focus) {
		background: var(--light-soft);
	}
	.style-color .app.tv .card[data-t='heat']:not(.focus) {
		background: var(--heat-soft);
	}
	.style-color .app.tv .card[data-t='cool']:not(.focus) {
		background: var(--cool-soft);
	}
	.style-color .app.tv .card[data-t='sea']:not(.focus) {
		background: var(--sea-soft);
	}
	.style-color .app.tv .card[data-t='media']:not(.focus) {
		background: var(--media-soft);
	}
	.style-color .app.tv .card[data-t='safe']:not(.focus) {
		background: var(--safe-soft);
	}
	.style-color .app.tv .card[data-t='accent']:not(.focus) {
		background: var(--accent-soft);
	}
	.hints {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		display: flex;
		justify-content: center;
		gap: 22px;
		font-size: 14px;
		color: var(--ink-2);
	}
	.hints b {
		display: inline-block;
		background: var(--surface);
		border-radius: 6px;
		padding: 2px 7px;
		margin-right: 6px;
		font-weight: 700;
		color: var(--ink);
	}
	@media (prefers-reduced-motion: reduce) {
		.mini-phone .app,
		.app.tv .card {
			transition: none;
		}
	}
</style>
