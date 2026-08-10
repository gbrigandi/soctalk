import { describe, expect, it } from 'vitest';
import {
	authorityHostForLlmBaseUrl,
	priceOverrideKey,
	providerKindForPriceOverride
} from '../api/tenants';
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

const LLM_PRICE_KEY_CASES = [
	[
		'openrouter hosted authority wins over served engine',
		'openai-compatible',
		'https://openrouter.ai/api/v1',
		'sglang',
		'openrouter:*:deepseek/deepseek-chat'
	],
	[
		'openrouter subdomain is label-bound',
		'openai-compatible',
		'https://gateway.openrouter.ai/api/v1',
		'sglang',
		'openrouter:*:deepseek/deepseek-chat'
	],
	[
		'hosted OpenAI wins over served engine',
		'openai-compatible',
		'https://api.openai.com/v1',
		'sglang',
		'openai:*:gpt-4o'
	],
	[
		'hosted OpenAI trailing root dot wins over served engine',
		'openai-compatible',
		'https://api.openai.com./v1',
		'sglang',
		'openai:*:gpt-4o'
	],
	[
		'hosted OpenAI unicode trailing root dot wins over served engine',
		'openai-compatible',
		'https://api.openai.com\u3002/v1',
		'sglang',
		'openai:*:gpt-4o'
	],
	[
		'hosted Anthropic authority wins under OpenAI-compatible protocol',
		'openai-compatible',
		'https://api.anthropic.com',
		'sglang',
		'anthropic:*:claude-sonnet-4-6'
	],
	[
		'self-hosted engine on custom host',
		'openai-compatible',
		'http://sglang.internal:8000/v1',
		'sglang',
		'self_hosted:*:qwen3-32b'
	],
	[
		'generic gateway without hosted authority',
		'openai-compatible',
		'https://novarouteai.com/v1',
		'',
		'openai_compatible:*:deepseek-v4-flash'
	],
	[
		'evil OpenRouter prefix is not hosted',
		'openai-compatible',
		'https://evilopenrouter.ai/api/v1',
		'sglang',
		'self_hosted:*:model-x'
	],
	[
		'evil OpenAI prefix is not hosted',
		'openai-compatible',
		'https://evilapi.openai.com/v1',
		'sglang',
		'self_hosted:*:model-x'
	],
	[
		'evil Anthropic prefix is not hosted',
		'openai-compatible',
		'https://notapi.anthropic.com',
		'sglang',
		'self_hosted:*:model-x'
	],
	[
		'percent encoded dots stay inert',
		'openai-compatible',
		'https://api%2eopenai%2ecom/v1',
		'sglang',
		'self_hosted:*:model-x'
	]
] as const;

describe('LLM price override key derivation', () => {
	it.each(LLM_PRICE_KEY_CASES)('%s', (_label, provider, baseUrl, engine, expected) => {
		const model = expected.split(':').slice(2).join(':');
		expect(priceOverrideKey(provider, baseUrl, engine, model)).toBe(expected);
	});

	it('folds exactly one trailing root dot in the authority', () => {
		expect(authorityHostForLlmBaseUrl('https://api.openai.com./v1')).toBe('api.openai.com');
		expect(authorityHostForLlmBaseUrl('https://api.openai.com../v1')).toBe(
			'api.openai.com.'
		);
		expect(
			providerKindForPriceOverride(
				'openai-compatible',
				'https://api.openai.com../v1',
				'sglang'
			)
		).toBe('self_hosted');
	});
});
