/**
 * Safe GFM rendering for MODEL-GENERATED text (#98). CSR-only.
 *
 * Threat model: assistant messages embed tenant SIEM data surfaced by tool
 * calls (alert titles, raw log lines), so the markdown source is treated as
 * attacker-controlled. A hostile log line is a prompt-injection vector into
 * whatever we render. Design consequences:
 *
 *  - Raw HTML in the markdown is ESCAPED, not interpreted. A log line that
 *    literally contains `<script>...</script>` renders as visible text, which
 *    is what a SOC analyst needs (seeing the hostile payload), and it removes
 *    the raw-HTML class/attribute surface entirely (no `<a class="fixed
 *    inset-0 z-50">` UI-spoofing overlays reusing app CSS).
 *  - Output is sanitized with an explicit ALLOWLIST before it can reach
 *    {@html}: allowed tags only, `href` the only attribute, links restricted
 *    to http/https/mailto (no tel/sms/cid/xmpp/data/javascript), and every
 *    surviving <a> forced to rel="noopener noreferrer" target="_blank".
 *  - <img> is forbidden: a markdown image is the classic zero-click LLM
 *    exfiltration channel (render -> automatic GET with data in the URL).
 *
 * Browser-only: DOMPurify needs a DOM and the app is CSR (ssr=false in
 * routes/+layout.ts; chat data loads in onMount). This module must not be
 * imported into an SSR-rendered path without an isomorphic DOM.
 *
 * This module is the ONLY place allowed to produce HTML for {@html} from
 * model text. Keep the invariant here, not at call sites.
 */
import DOMPurify from 'dompurify';
import { marked, type Tokens } from 'marked';

function escapeHtml(s: string): string {
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&#39;');
}

// Render raw HTML tokens as escaped, visible text instead of interpreting
// them. Covers both block and inline HTML tokens in marked v18.
marked.use({
	gfm: true,
	breaks: false,
	renderer: {
		html(token: Tokens.HTML | Tokens.Tag) {
			return escapeHtml(token.text);
		}
	}
});

const ALLOWED_TAGS = [
	'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'a',
	'ul', 'ol', 'li', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'hr',
	'table', 'thead', 'tbody', 'tr', 'th', 'td'
];

// href is the only attribute we keep; rel/target are added by the hook below.
const ALLOWED_ATTR = ['href'];

// DOMPurify's default minus the schemes we don't want (tel/sms/cid/xmpp/ftp...).
// Keeps http/https/mailto plus relative and anchor links; everything else on
// an <a href> is dropped, which safe-fails the link into plain text.
const ALLOWED_URI_REGEXP =
	/^(?:(?:https?|mailto):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i;

let hooked = false;
function ensureLinkPolicyHook() {
	if (hooked) return;
	hooked = true;
	DOMPurify.addHook('afterSanitizeAttributes', (node) => {
		if (node.tagName === 'A' && node.hasAttribute('href')) {
			node.setAttribute('rel', 'noopener noreferrer');
			node.setAttribute('target', '_blank');
		}
	});
}

/** Render untrusted GFM to sanitized HTML. Browser-only (SPA; ssr=false). */
export function renderMarkdown(text: string): string {
	ensureLinkPolicyHook();
	const raw = marked.parse(text, { async: false }) as string;
	return DOMPurify.sanitize(raw, {
		ALLOWED_TAGS,
		ALLOWED_ATTR,
		ALLOWED_URI_REGEXP,
		FORBID_TAGS: ['img', 'svg', 'style', 'iframe', 'form', 'input', 'math'],
		ALLOW_DATA_ATTR: false
	});
}
