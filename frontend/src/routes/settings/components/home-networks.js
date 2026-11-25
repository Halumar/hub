/** Further networks that count as at home: templates and light client checks (the hub decides). */
export const MAX_HOME_NETWORKS = 10;

export const TEMPLATES = {
	tailscale: { name: 'Tailscale', fixed: '100.64.0.0/10', ipv6: 'fd7a:115c:a1e0::/48' },
	wireguard: { name: 'WireGuard', example: '10.8.0.0/24' },
	zerotier: { name: 'ZeroTier', example: '10.147.17.0/24' },
	custom: { name: '', example: '172.16.10.0/24' },
};

/** Rough shape check before sending (IPv4 or IPv6 with a prefix). */
export function looksLikeCidr(value) {
	return /^[0-9a-fA-F.:]+\/\d{1,3}$/.test(value || '');
}

/** 100.64.0.0/10 (shared address space: Tailscale, but also mobile carrier NAT). */
export function inSharedRange(cidr) {
	const m = /^(\d+)\.(\d+)\.\d+\.\d+\/(\d+)$/.exec(cidr || '');
	if (!m) return false;
	const [a, b] = [Number(m[1]), Number(m[2])];
	return a === 100 && b >= 64 && b <= 127;
}
