// Pure locale/path helpers (#52) — no $app or DOM imports so `src/hooks.ts`
// (reroute) can use them in any context. Type-only import keeps the paraglide
// runtime out of the hook bundle.
import type { Locale } from '$lib/paraglide/runtime';

/** URL segment (lowercase) → canonical BCP-47 locale.
 *  `satisfies` makes this map COMPLETE at compile time: adding a locale to the
 *  paraglide settings without a segment entry here is a type error, instead of
 *  a locale that silently never routes (`/xx-yy/*` would 404 and the switcher
 *  would omit it). The label maps below get the same guarantee from their
 *  `Record<Locale, ...>` annotations. */
export const SEGMENT_TO_LOCALE: Record<string, Locale> = {
	'en-us': 'en-US',
	'pt-br': 'pt-BR',
	'es-419': 'es-419',
	'zh-cn': 'zh-CN',
	'fr-fr': 'fr-FR',
	'de-de': 'de-DE',
	'it-it': 'it-IT',
	'zh-tw': 'zh-TW'
} satisfies Record<Lowercase<Locale>, Locale>;

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
	'zh-CN': '简中',
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

/** Re-prefix `target` with whatever locale segment `pathname` carries.
 *  For legacy-path redirects (loads, window.location targets): `reroute`
 *  resolves `/zh-tw/old-path` onto the route but leaves the pathname
 *  prefixed, so a bare redirect target silently reverts the UI to en-US.
 *  en-US arrives unprefixed and stays unprefixed; an explicit `/en-us/...`
 *  keeps its prefix (accepted-for-symmetry, same as direct navigation).
 *  Query strings on `target` ride through untouched. */
export function relocalizedPath(pathname: string, target: string): string {
	const seg = segmentOf(pathname);
	return seg ? `/${seg}${target}` : target;
}

/** Strip a leading locale segment: `/pt-br/login` → `/login`. Identity for
 *  unprefixed paths. Use this for EVERY path comparison (active nav, guards). */
export function stripLocale(pathname: string): string {
	const seg = segmentOf(pathname);
	if (!seg) return pathname;
	const rest = pathname.slice(seg.length + 1);
	return rest === '' ? '/' : rest;
}
