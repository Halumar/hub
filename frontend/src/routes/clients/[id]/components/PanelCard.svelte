<script>
	/**
	 * The device in Home Assistant: last status (screen or “app
	 * active”, backlight, battery), which entities the device has in Home
	 * Assistant (decided by the rules kiosk, panel_remote and panel_sensors),
	 * on wall panels and TVs test buttons (same checks as Home
	 * Assistant, logged as “Admin UI”) and the battery protection blueprint of
	 * the integration. Every device except watches and guests gets the card.
	 */
	import { onMount, untrack } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { formatRelativeDate } from '$lib/utils.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { platformIcon } from '$lib/device-info.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';

	let { client } = $props();

	// Watches have no hub connection and report no status.
	const STATUS_PLATFORMS = ['ios', 'ipados', 'tvos', 'android', 'android_tablet', 'android_tv'];
	const TVS = ['tvos', 'android_tv'];
	const PHONES = ['ios', 'android'];
	// Reported capabilities (contract panel-status): shown as active / missing.
	const CAPABILITIES = ['pip', 'nfc', 'proximity_sensor', 'light_sensor', 'device_admin', 'device_owner', 'background_connection', 'charge_limit_detected'];

	const BUTTONS = [
		{ action: 'wake', icon: 'sun' },
		{ action: 'screensaver', icon: 'moon' },
		{ action: 'home', icon: 'home' },
		{ action: 'reload', icon: 'refresh' },
	];

	let card = $state(null);
	let error = $state('');
	let busy = $state('');
	let level = $state(60);

	let cid = $derived(client?.id);
	let panel = $derived(card?.panel || null);
	let actions = $derived(card?.supported_actions || []);
	let canBrightness = $derived(actions.includes('set_brightness'));
	let isTv = $derived(TVS.includes(card?.platform));
	let caps = $derived(CAPABILITIES.filter((c) => typeof panel?.[c] === 'boolean'));

	onMount(() => {
		const off = onBroadcast('client_status_updated', (d) => {
			if (d?.id === client?.id || d?.client_id === client?.id) load();
		});
		const offs = [off, onBroadcast('profiles_changed', () => load()), onBroadcast('client_config_updated', () => load())];
		return () => offs.forEach((o) => o());
	});

	$effect(() => {
		if (cid) untrack(load);
	});

	async function load() {
		try {
			card = await api.getClientPanel(client.id);
			if (typeof card?.panel?.brightness === 'number') level = Math.max(5, Math.round(card.panel.brightness * 100));
			error = '';
		} catch (e) {
			error = e?.message || t('devices.panel.loadError');
		}
	}

	function errorText(e) {
		const code = e?.code;
		const key = code ? `devices.panel.error.${code}` : '';
		const text = key ? t(key) : '';
		return text && text !== key ? text : e?.message || t('devices.panel.error.failed');
	}

	async function send(action, extra = {}) {
		busy = action;
		try {
			const res = await api.sendPanelCommand(client.id, { action, ...extra });
			if (card) card = { ...card, panel: res.panel ?? card.panel };
			addToast(t('devices.panel.done', { action: t(`devices.panel.action.${action}`) }), 'success');
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			busy = '';
		}
	}

	function screenLabel(s) {
		return s ? t(`devices.panel.screen.${s}`) : t('devices.panel.unknown');
	}

	function appLabel(s) {
		return s ? t(`devices.panel.app.${s === 'off' ? 'off' : 'active'}`) : t('devices.panel.unknown');
	}

	function statusLabel() {
		return card?.is_panel ? screenLabel(panel?.screen) : appLabel(panel?.screen);
	}
</script>

{#if card && STATUS_PLATFORMS.includes(card.platform) && client?.access !== 'guest'}
	<section class="card" data-testid="panel-card" aria-labelledby="panel-h">
		<div class="card-head">
			<Icon name={platformIcon(card.platform)} />
			<h2 id="panel-h" class="flex-1">{card.is_panel ? t('devices.panel.title') : t('devices.panel.titleDevice')}</h2>
			<span class="badge {card.is_connected ? 'badge-ok' : ''}" data-testid="panel-badge"><span class="dot"></span>{card.is_connected ? statusLabel() : t('devices.panel.offline')}</span>
		</div>

		{#if error}
			<Banner tone="error">{error}</Banner>
		{:else}
			<dl class="dl box">
				{#if card.is_panel}
					<dt>{t('devices.panel.screenLabel')}</dt>
					<dd data-testid="panel-screen">{screenLabel(panel?.screen)}</dd>
				{:else}
					<dt>{t('devices.panel.appLabel')}</dt>
					<dd data-testid="panel-app">{card.is_connected ? appLabel(panel?.screen) : t('devices.panel.offline')}</dd>
				{/if}
				{#if !isTv}
					<dt>{t('devices.panel.brightness')}</dt>
					<dd data-testid="panel-brightness">
						{typeof panel?.brightness === 'number' ? `${Math.round(panel.brightness * 100)} %` : '–'}{#if panel?.brightness_mode} · {t(`devices.panel.mode.${panel.brightness_mode}`)}{/if}
						{#if !card.is_panel}<span class="block t-help">{t('devices.panel.brightnessForeground')}</span>{/if}
					</dd>
					<dt>{t('devices.panel.battery')}</dt>
					<dd data-testid="panel-battery">
						{card.battery_level != null && card.battery_level >= 0 ? `${card.battery_level} %` : '–'}{#if card.battery_state} · {t(`devices.panel.batteryState.${card.battery_state}`)}{/if}{#if card.low_power} · {t('devices.panel.lowPower')}{/if}
					</dd>
				{/if}
				{#if typeof panel?.battery_temperature === 'number'}
					<dt>{t('devices.panel.batteryTemperature')}</dt>
					<dd data-testid="panel-battery-temperature">{`${panel.battery_temperature.toFixed(1)} °C`}{#if panel.battery_health}{` · ${t(`devices.panel.health.${panel.battery_health}`)}`}{/if}</dd>
				{/if}
				{#if caps.length}
					<dt>{t('devices.panel.capabilities')}</dt>
					<dd>
						<ul class="caps" data-testid="panel-capabilities">
							{#each caps as c (c)}
								<li><span class="dot {panel[c] ? 'on' : ''}" aria-hidden="true"></span>{t(`devices.panel.capability.${c}`)}: {panel[c] ? t('devices.panel.capOn') : t('devices.panel.capOff')}</li>
							{/each}
						</ul>
					</dd>
				{/if}
				<dt>{t('devices.panel.updated')}</dt>
				<dd>{card.updated_at ? formatRelativeDate(card.updated_at) : t('devices.panel.never')}</dd>
				<dt>{t('devices.panel.inHa')}</dt>
				<dd data-testid="panel-in-ha">
					{(card.panel_ha?.sensors || []).length ? card.panel_ha.sensors.map((s) => t(`devices.panel.haSensor.${s}`)).join(', ') : t('devices.panel.noSensors')}
					{#if card.is_panel}<span class="block t-help">{card.remote ? t('devices.panel.remoteOn') : t('devices.panel.remoteOff')}</span>{/if}
				</dd>
			</dl>

			{#if !card.is_panel}
				<p class="t-help mt-2" data-testid="panel-not-panel">{PHONES.includes(card.platform) ? t('devices.panel.notPanelPhone') : t('devices.panel.notPanel')}</p>
			{/if}

			{#if card.is_panel && card.remote}
				<h3 class="t-label mt-4">{t('devices.panel.test')}</h3>
				<div class="btn-grid mt-2" role="group" aria-label={t('devices.panel.test')}>
					{#each BUTTONS as b (b.action)}
						<button type="button" class="btn" disabled={!card.is_connected || !!busy || !actions.includes(b.action)} onclick={() => send(b.action)} data-testid="panel-btn-{b.action}">
							<Icon name={b.icon} /><span class="truncate">{t(`devices.panel.action.${b.action}`)}</span>
						</button>
					{/each}
				</div>
				{#if canBrightness}
					<div class="bright mt-2">
						<input type="range" min="5" max="100" step="5" bind:value={level} aria-label={t('devices.panel.brightness')} disabled={!card.is_connected || !!busy} />
						<span class="vnum">{level} %</span>
						<button type="button" class="btn" disabled={!card.is_connected || !!busy} onclick={() => send('set_brightness', { level: level / 100 })} data-testid="panel-btn-brightness">{t('devices.panel.setBrightness')}</button>
					</div>
				{/if}
				{#if !card.is_connected}<p class="t-help mt-2">{t('devices.panel.offlineHint')}</p>{/if}
				<p class="t-help mt-2">{t('devices.panel.whoHint')}</p>
			{/if}

			{#if card.is_panel && !isTv}
				<div class="blueprint mt-4" data-testid="panel-blueprint">
					<Icon name="battery" />
					<p class="t-help"><b>{t('devices.panel.blueprintTitle')}</b> {panel?.charge_limit_detected ? t('devices.panel.chargeLimit') : t('devices.panel.blueprintText')}</p>
				</div>
			{/if}
		{/if}
	</section>
{/if}

<style>
	/* Equal sizes: equal buttons in the grid, whatever the language. */
	.btn-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
		gap: 8px;
	}
	.btn-grid .btn {
		min-width: 0;
		justify-content: flex-start;
	}
	.bright {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 4.5em auto;
		gap: 10px;
		align-items: center;
	}
	.bright input {
		width: 100%;
		accent-color: var(--accent);
	}
	.vnum {
		font-weight: 650;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.blueprint {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 8px;
		align-items: start;
	}
	.blueprint p {
		margin: 0;
	}
	.caps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 2px;
	}
	.caps li {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.caps .dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--fill-3);
		flex: none;
	}
	.caps .dot.on {
		background: var(--ok);
	}
</style>
