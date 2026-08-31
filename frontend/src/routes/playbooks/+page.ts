import { redirect } from '@sveltejs/kit';
import { relocalizedPath } from '$lib/i18n/locales';

// The policy kind was renamed Playbooks -> Triage Policies. Keep the old URL working,
// and keep the visitor's locale prefix while doing it (see relocalizedPath).
export const load = ({ url }: { url: URL }) => {
	throw redirect(308, relocalizedPath(url.pathname, '/triage-policies'));
};
