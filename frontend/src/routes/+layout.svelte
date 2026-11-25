<script>
	import { BRAND } from '$lib/brand.ts';
	import '../app.css';
	import { base } from '$app/paths';
	import { page } from '$app/stores';
	import { afterNavigate } from '$app/navigation';
	import { onMount, tick } from 'svelte';
	import { api } from '$lib/api.js';
	import { ws } from '$lib/ws.js';
	import Toast from '$lib/components/Toast.svelte';
	import WsBadge from '$lib/components/WsBadge.svelte';
	import PairingDialog from '$lib/components/PairingDialog.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import BrandMark from '$lib/ui/BrandMark.svelte';
	import BrandWordmark from '$lib/ui/BrandWordmark.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { theme } from '$lib/theme.svelte.js';
	import { debounce } from '$lib/utils.js';
	// Initialize WS store (side-effect: wires up listeners)
	import { wsConnection, onBroadcast } from '$lib/stores/ws-store.svelte.js';

	let { children } = $props();

	// Auth state
	let authChecked = $state(false);
	let authenticated = $state(false);
	let isIngress = $state(false);
	let loginUsername = $state('');
	let loginPassword = $state('');
	let loginError = $state('');
	let loginLoading = $state(false);
	let sessionUsername = $state('');
	// Direct access outside Home Assistant (hub setting, default off).
	let directAccess = $state(true);
	let haUrl = $state('');

	let backendVersion = $state('');
	let activeGuests = $state(0);
	let unreadAlarms = $state(0);

	// Mobile drawer
	let drawerOpen = $state(false);
	let drawerEl = $state();
	let menuButton = $state();

	// Backend-offline banner: only after the socket has been lost for >10 s.
	let showOfflineBanner = $state(false);

	$effect(() => {
		// Only a lost connection counts as offline; 'limited' is shown by the badge.
		const lost = wsConnection.status === 'reconnecting' || wsConnection.status === 'disconnected';
		if (!lost) {
			showOfflineBanner = false;
			return;
		}
		const timer = setTimeout(() => {
			showOfflineBanner = true;
		}, 10000);
		return () => clearTimeout(timer);
	});

	// The admin WebSocket only runs while the UI is authenticated.
	$effect(() => {
		if (authChecked && authenticated) ws?.start();
		else ws?.stop();
	});

	$effect(() => {
		if (authChecked && authenticated) {
			loadGuestCount();
			loadAlarmCount();
		}
	});

	async function loadAlarmCount() {
		try {
			unreadAlarms = (await api.getLogSummary())?.unread_alarms || 0;
		} catch {
			// The badge is a hint only
		}
	}
	const refreshAlarmCount = debounce(loadAlarmCount, 400);

	async function loadVersion() {
		try {
			const health = await api.health();
			if (health.version) backendVersion = health.version;
		} catch {
			// Version display is not critical
		}
	}

	async function loadGuestCount() {
		try {
			const clients = await api.getPairedClients();
			const now = Date.now();
			activeGuests = clients.filter(
				(c) => c.access === 'guest' && !c.pending_deletion && (!c.expires_at || Date.parse(c.expires_at) > now),
			).length;
		} catch {
			// The badge is a hint only
		}
	}
	const refreshGuestCount = debounce(loadGuestCount, 400);

	let sessionExpiredNotified = false;
	function handleSessionExpired() {
		if (isIngress) {
			// Ingress auth belongs to Home Assistant; a login form makes no sense here.
			if (!sessionExpiredNotified) {
				sessionExpiredNotified = true;
				addToast(t('shell.haSessionInvalid'), 'error');
			}
			return;
		}
		if (!authenticated) return;
		authenticated = false;
		sessionUsername = '';
		loginError = t('shell.sessionExpired');
		// Direct access may have been switched off in the meantime.
		refreshDirectAccess();
	}

	async function refreshDirectAccess() {
		try {
			const session = await api.checkSession();
			directAccess = session.direct_access !== false;
			haUrl = safeHttpUrl(session.ha_url);
		} catch {
			// Keeps the login form
		}
	}

	function handleVisibility() {
		if (document.visibilityState === 'visible' && wsConnection.status === 'reconnecting') {
			ws?.reconnectNow();
		}
	}

	onMount(() => {
		const offI18n = i18n.init();
		const offTheme = theme.init();
		window.addEventListener('hub:session-expired', handleSessionExpired);
		const offWsExpired = ws?.on('session_expired', handleSessionExpired);
		document.addEventListener('visibilitychange', handleVisibility);
		const offs = ['client_paired', 'client_kicked', 'guests_expired', 'guest_extended', 'clients_cleared', 'resync'].map((type) =>
			onBroadcast(type, refreshGuestCount),
		);
		offs.push(
			onBroadcast('log_entry', (e) => e?.category === 'alarm' && refreshAlarmCount()),
			onBroadcast('log_read', refreshAlarmCount),
			onBroadcast('resync', refreshAlarmCount),
		);

		if (window.__INGRESS_PATH) {
			isIngress = true;
			authenticated = true;
			authChecked = true;
			loadVersion();
		} else {
			checkSession();
		}

		return () => {
			offI18n();
			offTheme();
			window.removeEventListener('hub:session-expired', handleSessionExpired);
			offWsExpired?.();
			document.removeEventListener('visibilitychange', handleVisibility);
			offs.forEach((off) => off());
			refreshGuestCount.cancel();
			refreshAlarmCount.cancel();
		};
	});

	async function checkSession() {
		try {
			const session = await api.checkSession();
			authenticated = session.authenticated === true;
			directAccess = session.direct_access !== false;
			haUrl = safeHttpUrl(session.ha_url);
			if (session.username) sessionUsername = session.username;
			if (authenticated) loadVersion();
		} catch {
			authenticated = false;
		}
		authChecked = true;
	}

	function safeHttpUrl(value) {
		try {
			const u = new URL(value);
			return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : '';
		} catch {
			return '';
		}
	}

	async function handleLogin(e) {
		e?.preventDefault();
		if (!loginUsername || !loginPassword) return;
		loginError = '';
		loginLoading = true;
		try {
			const result = await api.login(loginUsername, loginPassword);
			if (result.success) {
				authenticated = true;
				sessionUsername = result.username || loginUsername;
				loginUsername = '';
				loginPassword = '';
				loadVersion();
			}
		} catch (err) {
			if (err.reason === 'admin_ingress_only') {
				loginPassword = '';
				await refreshDirectAccess();
			} else {
				loginError = err.message || t('shell.loginFailed');
			}
		} finally {
			loginLoading = false;
		}
	}

	async function handleLogout() {
		try {
			await api.logout();
		} catch {
			// Ignore
		}
		authenticated = false;
		sessionUsername = '';
	}

	let currentPath = $derived.by(() => {
		const pathname = $page.url?.pathname || '/';
		const b = base || '';
		if (b && pathname.startsWith(b)) return pathname.slice(b.length) || '/';
		return pathname;
	});

	const navItems = [
		{ path: '/', key: 'dashboard', icon: 'dashboard' },
		{ path: '/areas', key: 'areas', icon: 'grid' },
		{ path: '/clients', key: 'devices', icon: 'iphone' },
		{ path: '/profiles', key: 'profiles', icon: 'template' },
		{ path: '/guests', key: 'guests', icon: 'users' },
		{ path: '/family', key: 'family', icon: 'child' },
		{ path: '/log', key: 'log', icon: 'history' },
		{ path: '/notifications', key: 'notifications', icon: 'bell' },
		{ path: '/settings', key: 'settings', icon: 'gear' },
	];

	function isActive(path) {
		const cp = currentPath;
		if (path === '/') return cp === '/' || cp === '';
		return cp === path || cp.startsWith(`${path}/`);
	}

	let currentLabel = $derived.by(() => {
		if (currentPath === '/setup' || currentPath.startsWith('/setup/')) return t('setup.title');
		const item = navItems.find((i) => isActive(i.path));
		return item ? t(`nav.${item.key}`) : '';
	});

	async function openDrawer() {
		drawerOpen = true;
		await tick();
		drawerEl?.querySelector('a, button')?.focus();
	}

	function closeDrawer(restoreFocus = true) {
		if (!drawerOpen) return;
		drawerOpen = false;
		if (restoreFocus) menuButton?.focus();
	}

	function onDrawerKey(e) {
		if (!drawerOpen) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			closeDrawer();
		} else if (e.key === 'Tab') {
			const items = [...drawerEl.querySelectorAll('a[href], button:not([disabled])')];
			if (!items.length) return;
			if (e.shiftKey && document.activeElement === items[0]) {
				e.preventDefault();
				items.at(-1).focus();
			} else if (!e.shiftKey && document.activeElement === items.at(-1)) {
				e.preventDefault();
				items[0].focus();
			}
		}
	}

	afterNavigate(() => closeDrawer(false));
</script>

<svelte:window onkeydown={onDrawerKey} />

<svelte:head>
	<title>{currentLabel ? `${currentLabel} · ${BRAND.name}` : BRAND.name}</title>
</svelte:head>

{#if showOfflineBanner}
	<div class="offline" role="alert">
		<BrandMark size={22} state="offline" />
		<span>{t('shell.offlineBanner')}</span>
		<button type="button" class="btn btn-sm" onclick={() => location.reload()}>{t('common.reload')}</button>
	</div>
{/if}

{#if !authChecked}
	<div class="center-screen">
		<div class="state" role="status">
			<BrandMark size={56} state="loading" />
			<p>{t('shell.loading')}</p>
		</div>
	</div>
{:else if !authenticated && !directAccess}
	<main class="center-screen">
		<div class="login text-center" data-testid="ingress-only">
			<h1 class="login-logo"><BrandWordmark height={40} /></h1>
			<div class="card stack p-6 mt-6">
				<p class="font-semibold">{t('shell.ingressOnly')}</p>
				<p class="t-help">{t('shell.ingressOnlyHint')}</p>
				{#if haUrl}
					<a class="btn btn-primary btn-block" href={haUrl} rel="noopener noreferrer">{t('shell.openHa')}</a>
				{/if}
			</div>
		</div>
	</main>
{:else if !authenticated}
	<main class="center-screen">
		<div class="login">
			<div class="text-center mb-6">
				<h1 class="login-logo"><BrandWordmark height={40} /></h1>
				<p class="t-sub">{t('shell.signInHint')}</p>
			</div>
			<form class="card stack p-6" onsubmit={handleLogin}>
				{#if loginError}<Banner tone="error">{loginError}</Banner>{/if}
				<Field label={t('shell.username')} id="login-username">
					{#snippet children({ id })}
						<input {id} class="input" type="text" bind:value={loginUsername} placeholder={t('shell.usernamePlaceholder')} autocomplete="username" disabled={loginLoading} required />
					{/snippet}
				</Field>
				<Field label={t('shell.password')} id="login-password">
					{#snippet children({ id })}
						<input {id} class="input" type="password" bind:value={loginPassword} autocomplete="current-password" disabled={loginLoading} required />
					{/snippet}
				</Field>
				<button type="submit" class="btn btn-primary btn-block" disabled={loginLoading || !loginUsername || !loginPassword}>
					{loginLoading ? t('shell.signingIn') : t('shell.signIn')}
				</button>
			</form>
			<p class="t-help text-center mt-4">
				{t('shell.apiDocs')} <a href="/api/docs" class="link">/api/docs</a>
			</p>
		</div>
	</main>
{:else}
	<a href="#main" class="skip">{t('shell.skipToContent')}</a>
	<div class="shell">
		<!-- Phones/tablets: slim bar. Under HA Ingress the HA toolbar already
		     shows the app name, so only menu + page title + live state. -->
		<div class="mobilebar" class:ingress={isIngress} data-testid="mobile-header">
			<button bind:this={menuButton} type="button" class="btn btn-ghost btn-icon" onclick={openDrawer} aria-label={t('nav.openMenu')} aria-expanded={drawerOpen ? 'true' : 'false'} aria-controls="sidebar">
				<Icon name="menu" />
			</button>
			{#if isIngress}
				<span class="mobilebar-title">{currentLabel}</span>
			{:else}
				<BrandWordmark height={20} />
				<span class="mobilebar-title t-muted">{currentLabel}</span>
			{/if}
			<WsBadge />
		</div>

		{#if drawerOpen}
			<!-- svelte-ignore a11y_click_events_have_key_events -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="scrim" onclick={() => closeDrawer()}></div>
		{/if}

		<aside id="sidebar" class="side" class:open={drawerOpen} bind:this={drawerEl} aria-label={t('nav.main')}>
			<div class="side-head">
				<h2 class="side-brand"><BrandWordmark height={22} /></h2>
				<small class="hub-tag">{t('nav.hub')}</small>
				<button type="button" class="btn btn-ghost btn-icon btn-sm ml-auto lg:hidden" onclick={() => closeDrawer()} aria-label={t('nav.closeMenu')}>
					<Icon name="x" />
				</button>
			</div>
			<nav class="side-nav" aria-label={t('nav.main')}>
				{#each navItems as item (item.path)}
					<a href="{base}{item.path}" aria-current={isActive(item.path) ? 'page' : undefined} data-testid="nav-{item.key}">
						<Icon name={item.icon} />
						<span class="flex-1">{t(`nav.${item.key}`)}</span>
						{#if item.key === 'log' && unreadAlarms > 0}
							<span class="cnt cnt-alarm" aria-label={t('nav.newAlarms', { count: unreadAlarms })}>{unreadAlarms}</span>
						{/if}
						{#if item.key === 'guests' && activeGuests > 0}
							<span class="cnt" aria-label={t('nav.activeGuests', { count: activeGuests })}>{activeGuests}</span>
						{/if}
					</a>
				{/each}
			</nav>
			<div class="side-foot">
				<WsBadge />
				<span>{backendVersion ? t('shell.version', { version: backendVersion }) : BRAND.name}</span>
				{#if !isIngress && sessionUsername}
					<span class="t-help">{t('shell.signedInAs', { name: sessionUsername })}</span>
					<button type="button" class="btn btn-sm justify-start" onclick={handleLogout}><Icon name="logout" />{t('shell.logout')}</button>
				{/if}
			</div>
		</aside>

		<main id="main" class="main" class:ingress={isIngress} tabindex="-1">
			{@render children()}
		</main>
	</div>
	<PairingDialog />
{/if}
<Toast />

<style>
	.center-screen {
		min-height: 100dvh;
		display: grid;
		place-items: center;
		padding: 16px;
		box-sizing: border-box;
	}
	.login {
		width: 100%;
		max-width: 380px;
	}
	.skip {
		position: fixed;
		left: 8px;
		top: -60px;
		z-index: 400;
		background: var(--ink);
		color: var(--surface);
		padding: 10px 14px;
		border-radius: var(--r-ctl);
		font-weight: 650;
	}
	.skip:focus {
		top: 8px;
	}
	.offline {
		position: fixed;
		inset: 0 0 auto 0;
		z-index: 250;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 8px 16px;
		background: var(--err-soft);
		color: var(--ink);
		font-weight: 600;
		border-bottom: 1px solid var(--line);
	}
	.shell {
		display: flex;
		height: 100dvh;
		overflow: hidden;
	}
	.side {
		width: var(--sidebar-w);
		flex: none;
		background: var(--surface);
		border-right: 1px solid var(--line);
		display: flex;
		flex-direction: column;
		padding: 16px 12px;
		box-sizing: border-box;
		overflow-y: auto;
	}
	.side-head {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 4px 8px 18px;
	}
	.side-brand {
		margin: 0;
	}
	/* Protection zone of the logo: at least one window (here about 12 px) around it. */
	.login-logo {
		display: flex;
		justify-content: center;
		margin: 0 0 var(--s4);
		padding: var(--s3);
	}
	.hub-tag {
		font-size: 11px;
		color: var(--ink-3);
		font-weight: 650;
		border-left: 1px solid var(--line-2);
		padding-left: 10px;
	}
	.side-nav {
		display: grid;
		gap: 2px;
	}
	.side-nav a {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 40px;
		padding: 0 12px;
		border-radius: var(--r-ctl);
		color: var(--ink-2);
		font-weight: 550;
		text-decoration: none;
	}
	.side-nav a :global(.ic) {
		width: 19px;
		height: 19px;
	}
	.side-nav a:hover {
		background: var(--fill);
		color: var(--ink);
	}
	.side-nav a[aria-current='page'] {
		background: var(--fill-2);
		color: var(--ink);
		font-weight: 650;
	}
	.cnt {
		font-size: 11px;
		font-weight: 700;
		background: var(--accent-soft);
		color: var(--accent-ink);
		border-radius: 8px;
		padding: 1px 7px;
	}
	.cnt-alarm {
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.side-foot {
		margin-top: auto;
		display: grid;
		gap: 8px;
		padding: 10px 12px 0;
		font-size: var(--fs-cap);
		color: var(--ink-3);
	}
	.main {
		flex: 1;
		min-width: 0;
		overflow-y: auto;
		padding: 24px 28px 48px;
		box-sizing: border-box;
	}
	.main:focus {
		outline: none;
	}
	.mobilebar {
		display: none;
	}
	.scrim {
		display: none;
	}

	@media (max-width: 1023px) {
		.shell {
			flex-direction: column;
		}
		.mobilebar {
			display: flex;
			align-items: center;
			gap: 10px;
			padding: 6px 12px;
			background: var(--surface);
			border-bottom: 1px solid var(--line);
			flex: none;
		}
		.mobilebar.ingress {
			padding: 2px 8px;
		}
		.mobilebar-title {
			flex: 1;
			min-width: 0;
			font-weight: 650;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		.side {
			position: fixed;
			inset: 0 auto 0 0;
			z-index: 150;
			transform: translateX(-100%);
			visibility: hidden;
			transition: transform 0.2s ease, visibility 0.2s;
			box-shadow: var(--shadow-modal);
		}
		.side.open {
			transform: none;
			visibility: visible;
		}
		.scrim {
			display: block;
			position: fixed;
			inset: 0;
			z-index: 140;
			background: var(--scrim);
		}
		.main {
			padding: 16px 14px 96px;
		}
		.side-nav a {
			min-height: 44px;
		}
	}
</style>
