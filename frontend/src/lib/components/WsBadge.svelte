<script>
	/** Honest live indicator: "Live" only for an authenticated admin socket. */
	import { wsConnection } from '$lib/stores/ws-store.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';

	const TONE = { connected: 'ok', limited: 'warn', connecting: 'warn', reconnecting: 'warn', disconnected: 'err', idle: 'idle' };
	let status = $derived(TONE[wsConnection.status] ? wsConnection.status : 'idle');
	let pulse = $derived(status === 'reconnecting' || status === 'connecting');
</script>

<span class="live live-{TONE[status]}" title={t(`shell.ws.${status}Title`)} data-ws-status={wsConnection.status}>
	<i class:pulse aria-hidden="true"></i>{t(`shell.ws.${status}`)}
</span>

<style>
	.live {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: var(--fs-cap);
		font-weight: 650;
		white-space: nowrap;
	}
	.live i {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex: none;
	}
	.live-ok {
		color: var(--ok-ink);
	}
	.live-ok i {
		background: var(--ok);
		box-shadow: 0 0 0 3px var(--ok-soft);
	}
	.live-warn {
		color: var(--warn-ink);
	}
	.live-warn i {
		background: var(--warn);
		box-shadow: 0 0 0 3px var(--warn-soft);
	}
	.live-err {
		color: var(--err-ink);
	}
	.live-err i {
		background: var(--err);
		box-shadow: 0 0 0 3px var(--err-soft);
	}
	.live-idle {
		color: var(--ink-3);
	}
	.live-idle i {
		background: var(--ink-3);
	}
	.pulse {
		animation: live-pulse 1.2s ease-in-out infinite;
	}
	@keyframes live-pulse {
		50% {
			opacity: 0.35;
		}
	}
</style>
