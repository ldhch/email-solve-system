const STYLES: Record<string, string> = {
  high: "bg-risk-high-tint text-risk-high",
  medium: "bg-risk-medium-tint text-risk-medium",
  low: "bg-risk-low-tint text-risk-low",
  unknown: "bg-[#EFF1F3] text-sub",
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
      className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${STYLES[key]}`}
    >
      {LABELS[key]}
    </span>
  );
}
