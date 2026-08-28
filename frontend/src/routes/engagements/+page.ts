import { redirect } from '@sveltejs/kit';
import { localeFromPathname, segmentForLocale } from '$lib/i18n/locales';

// Engagements were unified into the tenant Authorization area (an engagement is a kind of
// authorization). Keep the old path working as a deep link into the Engagements tab.
//
// The redirect MUST carry the locale prefix. `reroute` resolves /zh-tw/engagements onto this
// route but leaves url.pathname prefixed, so a bare '/my-authorization' target dropped the
// locale and bounced every non-English visitor onto the unprefixed (en-US) page — the one
// route in the app that silently reverted the UI language.
export const load = ({ url }) => {
	const locale = localeFromPathname(url.pathname);
	const segment = locale ? segmentForLocale(locale) : null;
	const prefix = segment ? `/${segment}` : '';
	throw redirect(308, `${prefix}/my-authorization?tab=engagements`);
};
