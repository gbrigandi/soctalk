import { redirect } from '@sveltejs/kit';
import { relocalizedPath } from '$lib/i18n/locales';

// Old editor URL kept working after the Playbooks -> Triage Policies rename,
// locale prefix included (see relocalizedPath).
export const load = ({ url }: { url: URL }) => {
	throw redirect(308, relocalizedPath(url.pathname, `/triage-policies/editor${url.search}`));
};
