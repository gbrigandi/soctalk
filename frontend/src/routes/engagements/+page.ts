import { redirect } from '@sveltejs/kit';
import { relocalizedPath } from '$lib/i18n/locales';

// Engagements were unified into the tenant Authorization area (an engagement is a kind of
// authorization). Keep the old path working as a deep link into the Engagements tab.
//
// The redirect MUST carry the locale prefix: `reroute` resolves /zh-tw/engagements onto this
// route but leaves url.pathname prefixed, so a bare '/my-authorization' target would drop the
// locale and bounce a non-English visitor onto the unprefixed (en-US) page. Every legacy-path
// redirect shares this trap — use relocalizedPath, never a bare target.
export const load = ({ url }: { url: URL }) => {
	throw redirect(308, relocalizedPath(url.pathname, '/my-authorization?tab=engagements'));
};
