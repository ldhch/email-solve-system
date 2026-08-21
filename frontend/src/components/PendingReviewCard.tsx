import { useState } from "react";
import { errorText, http } from "../api/client";
import { formatLocal } from "../utils/format";
import { TimelineItem } from "./Timeline";

// Aggregates the conversation's pending-review replies into one actionable
// card at the top of the pane, so the boss sees "what do I approve today" in a
// single glance instead of scanning the whole thread. Inline approve/reject
// controls in the timeline remain for acting in context.
export function PendingReviewCard({
  items,
  onRefresh,
}: {
  items: TimelineItem[];
  onRefresh: () => void;
}) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");

  const pending = items.filter(
    (t): t is TimelineItem & { reply_id: number } =>
      t.type === "reply" && t.status === "pending_review" && t.reply_id != null,
  );
  if (!pending.length) return null;

  async function act(replyId: number, approve: boolean) {
    setBusyId(replyId);
    setError("");
    try {
      await http.post(
        `/replies/${replyId}/${approve ? "approve" : "reject"}`,
        approve ? {} : { reason: "" },
      );
      onRefresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="mb-3 rounded-lg border border-accent/20 bg-accent-tint p-3">
      <h3 className="mb-2 text-[13px] font-semibold text-accent">
        待审核回复（{pending.length}）
      </h3>
      <ul className="space-y-2">
        {pending.map((t) => (
          <li
            key={t.reply_id}
            className="rounded border border-accent/15 bg-white px-3 py-2"
          >
            {t.low_confidence && (
              <p className="mb-1.5 inline-block rounded bg-[#FDF1DC] px-1.5 py-0.5 text-[11px] font-medium text-[#B45309]">
                置信度低 · 发送前请核对
              </p>
            )}
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 text-[12.5px] leading-normal text-ink line-clamp-2">
                {(t.content_cn || t.content_en || "").replace(/\s+/g, " ").trim()}
              </p>
              <span className="shrink-0 text-[11px] text-sub tabular-nums">
                {formatLocal(t.at ?? null)}
              </span>
            </div>
            <div className="mt-2 flex gap-2">
              <button
                disabled={busyId === t.reply_id}
                onClick={() => act(t.reply_id, true)}
                className="px-3 py-1.5 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90 disabled:opacity-50"
              >
                审核通过并发送
              </button>
              <button
                disabled={busyId === t.reply_id}
                onClick={() => act(t.reply_id, false)}
                className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink disabled:opacity-50"
              >
                驳回为草稿
              </button>
            </div>
          </li>
        ))}
      </ul>
      {error && <p className="mt-2 text-[12px] text-risk-high">{error}</p>}
    </div>
  );
}
