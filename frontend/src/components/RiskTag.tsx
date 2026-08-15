const STYLES: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-yellow-100 text-yellow-700",
  low: "bg-green-100 text-green-700",
  unknown: "bg-gray-100 text-gray-600",
};

const LABELS: Record<string, string> = {
  high: "高风险",
  medium: "中风险",
  low: "低风险",
  unknown: "无法判定",
};

export function RiskTag({ risk }: { risk?: string | null }) {
  const key = risk && STYLES[risk] ? risk : "unknown";
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${STYLES[key]}`}
    >
      {LABELS[key]}
    </span>
  );
}
