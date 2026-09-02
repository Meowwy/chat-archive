const LOCALE = 'en-GB';

const time = new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit' });
const day = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'long', year: 'numeric' });
const dayShort = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'numeric', year: 'numeric' });
const monthName = new Intl.DateTimeFormat(LOCALE, { month: 'short', year: 'numeric' });
const full = new Intl.DateTimeFormat(LOCALE, { dateStyle: 'full', timeStyle: 'medium' });

export const formatTime = (ms) => time.format(new Date(ms));
export const formatDay = (ms) => day.format(new Date(ms));
export const formatDayShort = (ms) => dayShort.format(new Date(ms));
export const formatFull = (ms) => full.format(new Date(ms));
export const formatMonth = (key) => monthName.format(new Date(`${key}-01T00:00:00Z`));

export const dayKey = (ms) => {
	const d = new Date(ms);
	return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};

export const formatCount = (n) => new Intl.NumberFormat(LOCALE).format(n ?? 0);

export function formatSize(bytes) {
	if (!bytes) return '';
	const units = ['B', 'kB', 'MB', 'GB'];
	let value = bytes;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit++;
	}
	return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function relativeDay(ms) {
	const now = new Date();
	const then = new Date(ms);
	const days = Math.floor((now - then) / 86400000);
	if (days === 0) return 'today';
	if (days === 1) return 'yesterday';
	if (days < 7) return `${days} days ago`;
	return dayShort.format(then);
}

export const PLATFORMS = {
	discord: { label: 'Discord', color: '#5865f2' },
	facebook: { label: 'Facebook', color: '#0866ff' },
	instagram: { label: 'Instagram', color: '#e1306c' }
};

export const platformLabel = (key) => PLATFORMS[key]?.label ?? key;
export const platformColor = (key) => PLATFORMS[key]?.color ?? '#888';

const URL_RE = /(https?:\/\/[^\s<]+)/g;

/**
 * Split message text into plain and link segments for safe rendering.
 * Never returns HTML - the template renders each part as text.
 */
export function linkify(text) {
	if (!text) return [];
	const parts = [];
	let last = 0;
	for (const match of text.matchAll(URL_RE)) {
		if (match.index > last) parts.push({ type: 'text', value: text.slice(last, match.index) });
		parts.push({ type: 'link', value: match[0] });
		last = match.index + match[0].length;
	}
	if (last < text.length) parts.push({ type: 'text', value: text.slice(last) });
	return parts;
}
