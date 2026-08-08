import { describe, expect, it } from 'vitest';
import { detectSlugFromHostname } from './index';

describe('detectSlugFromHostname', () => {
	it('takes the leftmost label as the slug', () => {
		expect(detectSlugFromHostname('labz.soctalk.ai')).toBe('labz');
		expect(detectSlugFromHostname('pw-kmo36q.soctalk.ai')).toBe('pw-kmo36q');
	});

	it('returns null for reserved subdomains', () => {
		for (const h of ['www.soctalk.ai', 'api.soctalk.ai', 'app.soctalk.ai']) {
			expect(detectSlugFromHostname(h)).toBeNull();
		}
	});

	it('returns null for localhost, bare IPs and single labels', () => {
		expect(detectSlugFromHostname('localhost')).toBeNull();
		expect(detectSlugFromHostname('100.102.223.8')).toBeNull();
		expect(detectSlugFromHostname('')).toBeNull();
		expect(detectSlugFromHostname(null)).toBeNull();
	});

	// #136: the bare-IP guard was not enough. Wildcard DNS that embeds an
	// address is how a bare-IP install gets a hostname, and it degrades to a
	// numeric first label — which used to be accepted, producing a 404 on
	// /api/public/scope-by-slug/100 for every page load.
	it('returns null for IP-embedding wildcard DNS hosts', () => {
		expect(detectSlugFromHostname('100.102.223.8.nip.io')).toBeNull();
		expect(detectSlugFromHostname('10.0.0.1.sslip.io')).toBeNull();
		expect(detectSlugFromHostname('192.168.1.50.xip.io')).toBeNull();
	});

	it('returns null for an all-digit leading label', () => {
		expect(detectSlugFromHostname('100.soctalk.ai')).toBeNull();
		expect(detectSlugFromHostname('42.example.com')).toBeNull();
	});

	it('still accepts a slug that merely contains digits', () => {
		expect(detectSlugFromHostname('tenant1.soctalk.ai')).toBe('tenant1');
		expect(detectSlugFromHostname('a1b2.soctalk.ai')).toBe('a1b2');
	});
});
