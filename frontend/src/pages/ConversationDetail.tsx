import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { PendingReviewCard } from "../components/PendingReviewCard";
import { ReplyDraftEditor } from "../components/ReplyDraftEditor";
import { ReplyEditor } from "../components/ReplyEditor";
import { RiskTag } from "../components/RiskTag";
import { Timeline, TimelineItem } from "../components/Timeline";
import { formatLocal } from "../utils/format";
import { loadShowCn, saveShowCn } from "../utils/langPref";

interface ConversationData {
  id: number;
  subject: string;
  customer: { email: string; display_name: string | null };
  status: string;
  risk_level: string | null;
  retention_attempts: number;
  sla_deadline: string | null;
  timeline: TimelineItem[];
}

const STATUS_LABEL: Record<string, string> = {
  open: "进行中",
  escalated: "人工介入",
  resolved: "已解决",
};

export default function ConversationDetail() {
  const { id } = useParams();
  const conversationId = Number(id);
  const [data, setData] = useState<ConversationData | null>(null);
  const [showCn, setShowCn] = useState(loadShowCn);
  // Conversation-level 概括/全文 toggle: summary shows only the latest
  // customer email's digest; full shows its complete letter on a gray board.
  const [convMode, setConvMode] = useState<"summary" | "full">("summary");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await http.get(`/conversations/${conversationId}`);
      setData(dataOf<ConversationData>(resp));
    } catch (err) {
      setError(errorText(err));
    }
  }, [conversationId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000); // F-02: 5s polling, no websocket
    return () => clearInterval(timer);
  }, [load]);

  if (error && !data) {
    return (
      <Layout>
        <p className="text-red-600">{error}</p>
      </Layout>
    );
  }
  if (!data) {
    return (
      <Layout>
        <p className="text-gray-500">加载中…</p>
      </Layout>
    );
  }

  // Reply-box status: the last message decides whether the ball is in the
  // customer's court (we sent the last reply) or ours (latest is an email).
  const latestMsg = (data.timeline ?? [])
    .filter(
      (t) =>
        t.type === "email" || (t.type === "reply" && t.status === "sent"),
    )
    .sort((a, b) => (a.at ?? "").localeCompare(b.at ?? ""))
    .pop();
  const waitingForCustomer = latestMsg?.type === "reply";

  return (
    <Layout>
      <div className="mb-4">
        <h1 className="text-lg font-bold">{data.subject}</h1>
        <div className="flex flex-wrap items-center gap-3 mt-2 text-sm text-gray-600">
          <span>
            {data.customer.display_name || "客户"}（{data.customer.email}）
          </span>
          <RiskTag risk={data.risk_level} />
          <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-700">
            {STATUS_LABEL[data.status] || data.status}
          </span>
          {data.sla_deadline && (
            <span className="text-red-600">
              请在 {formatLocal(data.sla_deadline)} 前回复
            </span>
          )}
          <div className="ml-auto flex items-center bg-white border border-gray-300 rounded-md p-0.5">
            {(["summary", "full"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setConvMode(m)}
                className={`px-3 py-1 rounded text-sm leading-none transition-colors ${
                  convMode === m
                    ? "bg-accent text-white font-medium"
                    : "text-gray-500 hover:text-gray-800"
                }`}
              >
                {m === "summary" ? "概括" : "全文"}
              </button>
            ))}
          </div>
          <button
            onClick={() =>
              setShowCn((v) => {
                saveShowCn(!v);
                return !v;
              })
            }
            className="px-3 py-1 border border-gray-300 rounded text-sm"
          >
            {showCn ? "显示英文" : "显示中文"}
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}

      <Timeline
        items={data.timeline}
        showCn={showCn}
        mode={convMode}
        customerEmail={data.customer.email}
      />

      <ReplyDraftEditor items={data.timeline} onChanged={load} />

      {/* The pending-review card lives right above the manual reply box so
          approving a draft and writing a follow-up happen in one place. */}
      <PendingReviewCard items={data.timeline} onRefresh={load} />

      <div className="mt-4">
        {latestMsg && (
          <div
            className={`mb-2 flex items-center gap-1.5 rounded-lg border px-3 py-2 text-[12.5px] ${
              waitingForCustomer
                ? "border-line bg-[#F7F9FB] text-sub"
                : "border-accent/20 bg-accent-tint text-accent"
            }`}
          >
            {waitingForCustomer ? (
              <>√ 已发送 · 等待客户回复，有新来信后继续</>
            ) : (
              <>待回复 · 客户最新来信尚未处理</>
            )}
          </div>
        )}
        <ReplyEditor conversationId={data.id} onSent={load} />
      </div>
    </Layout>
  );
}
