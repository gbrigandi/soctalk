// Pure locale/path helpers (#52) — no $app or DOM imports so `src/hooks.ts`
// (reroute) can use them in any context. Type-only import keeps the paraglide
// runtime out of the hook bundle.
import type { Locale } from '$lib/paraglide/runtime';

/** URL segment (lowercase) → canonical BCP-47 locale. */
export const SEGMENT_TO_LOCALE: Record<string, Locale> = {
	'en-us': 'en-US',
	'pt-br': 'pt-BR',
	'es-419': 'es-419',
	'zh-cn': 'zh-CN',
	'fr-fr': 'fr-FR',
	'de-de': 'de-DE',
	'it-it': 'it-IT',
	'zh-tw': 'zh-TW'
};

/** Native-name labels for the switcher — deliberately NOT translated. */
export const LOCALE_LABELS: Record<Locale, string> = {
	'en-US': 'English',
	'pt-BR': 'Português (Brasil)',
	'es-419': 'Español (Latinoamérica)',
	'zh-CN': '中文（简体）',
	'fr-FR': 'Français',
	'de-DE': 'Deutsch',
	'it-IT': 'Italiano',
	'zh-TW': '中文（繁體）'
};

/** Compact codes for the collapsed switcher trigger in the narrow nav rail
 *  (the full endonyms above are shown untruncated in the open menu). */
export const LOCALE_SHORT: Record<Locale, string> = {
	'en-US': 'EN',
	'pt-BR': 'PT',
	'es-419': 'ES',
	'zh-CN': '中文',
	'fr-FR': 'FR',
	'de-DE': 'DE',
	'it-IT': 'IT',
	'zh-TW': '繁中'
};

export const SUPPORTED_LOCALES = Object.values(SEGMENT_TO_LOCALE) as Locale[];

export function segmentOf(pathname: string): string | null {
	const first = pathname.split('/')[1]?.toLowerCase() ?? '';
	return first in SEGMENT_TO_LOCALE ? first : null;
}

export function segmentForLocale(locale: Locale): string | null {
	return Object.keys(SEGMENT_TO_LOCALE).find((s) => SEGMENT_TO_LOCALE[s] === locale) ?? null;
}

/** The locale a pathname explicitly carries, or null if unprefixed. */
export function localeFromPathname(pathname: string): Locale | null {
	const seg = segmentOf(pathname);
	return seg ? SEGMENT_TO_LOCALE[seg] : null;
}

/** Strip a leading locale segment: `/pt-br/login` → `/login`. Identity for
 *  unprefixed paths. Use this for EVERY path comparison (active nav, guards). */
export function stripLocale(pathname: string): string {
	const seg = segmentOf(pathname);
	if (!seg) return pathname;
	const rest = pathname.slice(seg.length + 1);
	return rest === '' ? '/' : rest;
}
