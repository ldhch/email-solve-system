import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

// Aggregates every pending_review draft into one actionable list — medium-risk
// drafts, high-risk follow-up suggestions and low-confidence drafts. Each card
// shows the full draft (CN/EN toggle) plus the conversation's risk tag so the
// boss can approve, reject, or jump into the conversation to edit, all in one
// place. Sent always uses the English content; Chinese is for display only.
interface QueueItem {
  reply_id: number;
  conversation_id: number;
  subject: string | null;
  from_email: string | null;
  customer_name: string | null;
  content_cn: string | null;
  content_en: string | null;
  reply_type: string;
  low_confidence: boolean;
  source: string;
  risk_level: string;
  created_at: string | null;
  waiting_hours: number;
}

const RISK_LABEL: Record<string, { text: string; cls: string }> = {
  high: { text: "高风险", cls: "bg-red-50 text-red-700" },
  medium: { text: "中风险", cls: "bg-amber-50 text-amber-700" },
  low: { text: "低风险", cls: "bg-green-50 text-green-700" },
  unknown: { text: "无法判定", cls: "bg-gray-100 text-sub" },
};

export default function ReviewQueue() {
  const navigate = useNavigate();
  const [items, setItems] = useState<QueueItem[]>([]);
  const [total, setTotal] = useState(0);
  const [langs, setLangs] = useState<Record<number, "cn" | "en">>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/review-queue");
      const data = dataOf<{ items: QueueItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
      setError("");
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function act(replyId: number, approve: boolean) {
    setBusyId(replyId);
    setError("");
    try {
      await http.post(
        `/replies/${replyId}/${approve ? "approve" : "reject"}`,
        approve ? {} : { reason: "" },
      );
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  function waitingLabel(hours: number): { label: string; cls: string } {
    if (hours >= 24) {
      return { label: `已等待 ${Math.floor(hours)}h · 请尽快处理`, cls: "bg-red-50 text-red-600" };
    }
    if (hours >= 12) {
      return { label: `已等待 ${Math.floor(hours)}h`, cls: "bg-amber-50 text-amber-700" };
    }
    return { label: hours < 1 ? "刚生成" : `已等待 ${Math.floor(hours)}h`, cls: "text-sub" };
  }

  return (
    <Layout>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">待审核工作台</h1>
        <span className="text-sm text-sub">{total} 条待审</span>
      </div>

      {error && <p className="mb-4 text-sm text-risk-high">{error}</p>}

      {!items.length && !error && (
        <p className="py-10 text-center text-sm text-sub">暂无待审核草稿 🎉</p>
      )}

      <div className="space-y-3">
        {items.map((it) => {
          const isEn = langs[it.reply_id] === "en";
          const body = isEn
            ? it.content_en || it.content_cn || ""
            : it.content_cn || it.content_en || "";
          const risk = RISK_LABEL[it.risk_level] ?? RISK_LABEL.unknown;
          const wait = waitingLabel(it.waiting_hours);
          return (
            <div
              key={it.reply_id}
              className="rounded-lg border border-line bg-white p-4"
            >
              {/* Meta row: risk tag + confidence + sender + waiting time */}
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span
                  className={`inline-block rounded px-2 py-0.5 text-[11px] font-medium ${risk.cls}`}
                >
                  {risk.text}
                </span>
                {it.low_confidence && (
                  <span className="inline-block rounded bg-[#FDF1DC] px-1.5 py-0.5 text-[11px] font-medium text-[#B45309]">
                    置信度低 · 发送前请核对
                  </span>
                )}
                {it.source === "manual" && (
                  <span className="inline-block rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-sub">
                    人工
                  </span>
                )}
                <span className="text-[11px] text-sub">
                  {it.customer_name || it.from_email || "未知发件人"}
                  {it.subject ? ` · ${it.subject}` : ""}
                </span>
                <span
                  className={`ml-auto rounded px-1.5 py-0.5 text-[11px] tabular-nums ${wait.cls}`}
                >
                  {wait.label}
                </span>
              </div>

              {/* Full draft body with CN/EN toggle */}
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-sub">
                  回复草稿（{isEn ? "英文原文" : "中文"}）
                </span>
                <button
                  onClick={() =>
                    setLangs((prev) => ({
                      ...prev,
                      [it.reply_id]: isEn ? "cn" : "en",
                    }))
                  }
                  className="rounded border border-line px-1.5 py-0.5 text-[11px] text-sub hover:text-ink"
                >
                  {isEn ? "显示中文" : "显示英文"}
                </button>
              </div>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-ink">
                {body}
              </p>

              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  disabled={busyId === it.reply_id}
                  onClick={() => act(it.reply_id, true)}
                  className="px-3 py-1.5 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90 disabled:opacity-50"
                >
                  审核通过并发送
                </button>
                <button
                  disabled={busyId === it.reply_id}
                  onClick={() => act(it.reply_id, false)}
                  className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink disabled:opacity-50"
                >
                  驳回为草稿
                </button>
                <button
                  onClick={() => navigate(`/conversations/${it.conversation_id}`)}
                  className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink"
                >
                  打开会话编辑
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </Layout>
  );
}
