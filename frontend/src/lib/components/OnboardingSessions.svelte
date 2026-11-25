<script>
	/** Devices waiting to be set up from another device: name, device, who sets it up, time left, end. Hidden when none. */
	import { onMount, onDestroy } from 'svelte';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import { deviceIcon } from '$lib/device-info.js';

	let sessions = $state([]);
	let now = $state(Date.now());
	let busy = $state(null);
	let unsubs = [];
	let timer;

	async function load() {
		try {
			const r = await api.getOnboardingSessions();
			sessions = r.sessions || [];
		} catch {
			/* not critical: the list stays as it was */
		}
	}

	onMount(() => {
		load();
		timer = setInterval(() => (now = Date.now()), 1000);
		unsubs.push(
			onBroadcast('onboarding_started', load),
			onBroadcast('onboarding_claimed', load),
			onBroadcast('onboarding_ended', (data) => {
				if (data?.result === 'confirmed') {
					const s = sessions.find((x) => x.id === data.id);
					if (s) addToast(t('devices.onboardingConfirmed', { name: s.client_name }), 'success');
				}
				load();
			}),
			onBroadcast('client_config_updated', load),
			onBroadcast('resync', load),
		);
	});

	onDestroy(() => {
		clearInterval(timer);
		unsubs.forEach((fn) => fn());
	});

	let visible = $derived(sessions.filter((s) => new Date(s.expires_at).getTime() > now));

	function left(s) {
		const sec = Math.max(0, Math.round((new Date(s.expires_at).getTime() - now) / 1000));
		return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
	}

	async function cancel(s) {
		busy = s.id;
		try {
			await api.cancelOnboarding(s.id);
			addToast(t('devices.onboardingCancelled', { name: s.client_name }), 'success');
			await load();
		} catch (e) {
			addToast(t('devices.onboardingCancelFailed', { message: e.message }), 'error');
		} finally {
			busy = null;
		}
	}
</script>

{#if visible.length}
	<section class="card onb" aria-labelledby="onb-h" data-testid="onboarding-sessions">
		<div class="card-head">
			<Icon name="qr" />
			<h2 id="onb-h" class="flex-1">{t('devices.onboardingTitle')}</h2>
			<span class="badge">{visible.length}</span>
		</div>
		<p class="t-help mb-2">{t('devices.onboardingHint')}</p>
		<ul class="list">
			{#each visible as s (s.id)}
				<li class="item" data-testid="onboarding-{s.id}">
					<Icon name={deviceIcon({ platform: s.platform, name: s.client_name })} />
					<div class="min-w-0 flex-1">
						<div class="name">{s.client_name}</div>
						<div class="t-help">
							{[s.platform ? t(`devices.platforms.${s.platform}`) : null, s.device_model, s.app_version].filter(Boolean).join(' · ')}
							{#if s.source_ip}· {t('devices.onboardingFrom', { ip: s.source_ip })}{/if}
						</div>
						{#if s.claimed_by}<div class="t-help" data-testid="onboarding-claimed">{t('devices.onboardingClaimed', { name: s.claimed_by.name })}</div>{/if}
					</div>
					<span class="badge badge-warn" aria-live="off">{t('devices.onboardingLeft', { time: left(s) })}</span>
					<button type="button" class="btn btn-sm" aria-label={t('devices.onboardingCancelFor', { name: s.client_name })} disabled={busy === s.id} onclick={() => cancel(s)}>{t('devices.onboardingCancel')}</button>
				</li>
			{/each}
		</ul>
	</section>
{/if}

<style>
	.onb {
		margin-bottom: 12px;
	}
	.list {
		display: grid;
		gap: 8px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.item {
		display: flex;
		gap: 10px;
		align-items: center;
		padding: 8px 10px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-ctl);
		background: var(--surface-2);
	}
	.item :global(.ic) {
		color: var(--ink-2);
		width: 20px;
		height: 20px;
		flex: none;
	}
	.name {
		font-weight: 650;
		overflow-wrap: anywhere;
	}
</style>
