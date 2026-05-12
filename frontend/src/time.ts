/**
 * Local timestamp formatting helpers.
 */

function normalizeTimestamp(timestamp: string): string {
  if (!timestamp) return timestamp;
  if (/[zZ]$|[+-]\d{2}:\d{2}$/.test(timestamp)) return timestamp;
  return `${timestamp}Z`;
}

export function formatLocalTime(timestamp: string): string {
  const date = new Date(normalizeTimestamp(timestamp));
  if (Number.isNaN(date.getTime())) return '';

  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date);
}

export function formatLocalDateTime(timestamp: string): string {
  const date = new Date(normalizeTimestamp(timestamp));
  if (Number.isNaN(date.getTime())) return '';

  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}
