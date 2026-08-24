const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function toShanghai(d: Date): Date {
  return new Date(d.getTime() + SHANGHAI_OFFSET_MS);
}

function shanghaiParts(d: Date): {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
} {
  const s = toShanghai(d);
  return {
    year: s.getUTCFullYear(),
    month: s.getUTCMonth() + 1,
    day: s.getUTCDate(),
    hour: s.getUTCHours(),
    minute: s.getUTCMinutes(),
    second: s.getUTCSeconds(),
  };
}

export function formatLocal(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = shanghaiParts(d);
  return `${pad(p.month)}-${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}`;
}

export function formatFullLocal(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = shanghaiParts(d);
  return (
    `${p.year}-${pad(p.month)}-${pad(p.day)} ` +
    `${pad(p.hour)}:${pad(p.minute)}:${pad(p.second)} · UTC+8`
  );
}

export function formatSmartLocal(
  iso: string | null,
  now: Date = new Date(),
): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffMs = now.getTime() - d.getTime();
  if (diffMs >= 0 && diffMs < 60_000) return "刚刚";
  if (diffMs >= 0 && diffMs < 3_600_000) {
    return `${Math.floor(diffMs / 60_000)} 分钟前`;
  }
  if (diffMs >= 0 && diffMs < 86_400_000) {
    return `${Math.floor(diffMs / 3_600_000)} 小时前`;
  }

  const nowP = shanghaiParts(now);
  const dP = shanghaiParts(d);
  const nowDay = Date.UTC(nowP.year, nowP.month - 1, nowP.day);
  const dDay = Date.UTC(dP.year, dP.month - 1, dP.day);
  const dayDiff = Math.round((nowDay - dDay) / 86_400_000);
  if (dayDiff === 1) return "昨天";
  if (dP.year === nowP.year) return `${pad(dP.month)}-${pad(dP.day)}`;
  return `${dP.year}-${pad(dP.month)}-${pad(dP.day)}`;
}
