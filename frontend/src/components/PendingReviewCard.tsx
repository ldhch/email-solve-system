import { useState } from "react";
import { errorText, http } from "../api/client";
import { formatFullLocal, formatSmartLocal } from "../utils/format";
import { TimelineItem } from "./Timeline";

// Aggregates the conversation's pending-review replies into one actionable
// card sitting right above the manual reply box, so approving a draft and
// writing a follow-up happen in one place. Each draft shows everything the
// boss needs to judge it without leaving the card: the full draft body
// (CN/EN toggle), an inline editor that saves WITHOUT leaving the review
// queue (PATCH keeps status=pending_review), and the decision tags explaining
// why the draft exists. The customer thread sits right above on the timeline,
// so no email context is lost.
const REPLY_TYPE_LABEL: Record<string, string> = {
  general: "通用回复",
  retention_compensation: "补偿挽留",
  retention_exchange: "换货挽留",
  retention_release: "退款放行",
  review: "待审草稿",
};

export function PendingReviewCard({
  items,
  onRefresh,
}: {
  items: TimelineItem[];
  onRefresh: () => void;
}) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState("");
  // Per-draft language toggle: which side of the draft is shown.
  const [lang, setLang] = useState<Record<number, "cn" | "en">>({});
  // Inline edit state: which draft is being edited and its current text.
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editText, setEditText] = useState("");

  const pending = items.filter(
    (t): t is TimelineItem & { reply_id: number } =>
      t.type === "reply" && t.status === "pending_review" && t.reply_id != null,
  );
  if (!pending.length) return null;

  function waitingHours(iso: string | null): number {
    if (!iso) return 0;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return 0;
    return Math.max(0, Math.floor((Date.now() - d.getTime()) / 3_600_000));
  }

  // Save edits to a review draft in place. PATCH /replies/{id} accepts
  // pending_review drafts and keeps their status, so an edited draft stays in
  // the review queue instead of vanishing from it.
  async function saveEdit(replyId: number) {
    const content = editText.trim();
    if (!content || busyId != null) return;
    setError("");
    setBusyId(replyId);
    try {
      await http.patch(`/replies/${replyId}`, { content_cn: content });
      setEditingId(null);
      setEditText("");
      onRefresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

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

  // Removing a draft from the review queue demotes it to a plain draft, which
  // then no longer appears under the「待审核」filter — confirm the consequence
  // so the boss doesn't mistake the disappearance for data loss.
  function confirmReject(replyId: number) {
    if (
      window.confirm(
        "把这条草稿移出待审核列表（降为普通草稿）。之后可在该会话中继续编辑并发送。确定移出吗？",
      )
    ) {
      act(replyId, false);
    }
  }

  return (
    <div className="mt-4 rounded-lg border border-accent/20 bg-accent-tint p-3">
      <h3 className="mb-2 text-[13px] font-semibold text-accent">
        待审核回复（{pending.length}）
      </h3>
      <ul className="space-y-2">
        {pending.map((t) => {
          const isEn = lang[t.reply_id] === "en";
          const body = isEn
            ? t.content_en || t.content_cn || ""
            : t.content_cn || t.content_en || "";
          const hours = waitingHours(t.at ?? null);
          const autoReleases = t.reply_type === "retention_compensation";
          const timeLabel =
            hours >= 24
              ? autoReleases
                ? `已等待 ${hours}h · 超过24h将自动放行`
                : `已等待 ${hours}h · 不会自动发送`
              : hours >= 12
                ? `已等待 ${hours}h`
                : formatSmartLocal(t.at ?? null);
          const timeClass =
            hours >= 24
              ? "bg-red-50 text-red-600"
              : hours >= 12
                ? "bg-amber-50 text-amber-700"
                : "text-sub";
          return (
            <li
              key={t.reply_id}
              className="rounded border border-accent/15 bg-white px-3 py-2"
            >
              {/* Meta row: confidence + decision tags + time */}
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                {t.low_confidence && (
                  <span className="inline-block rounded bg-[#FDF1DC] px-1.5 py-0.5 text-[11px] font-medium text-[#B45309]">
                    置信度低 · 发送前请核对
                  </span>
                )}
                {t.reply_type && REPLY_TYPE_LABEL[t.reply_type] && (
                  <span className="inline-block rounded bg-accent-tint px-1.5 py-0.5 text-[11px] font-medium text-accent">
                    {REPLY_TYPE_LABEL[t.reply_type]}
                  </span>
                )}
                {t.source === "manual" && (
                  <span className="inline-block rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-sub">
                    人工
                  </span>
                )}
                <span
                  className={`ml-auto rounded px-1.5 py-0.5 text-[11px] tabular-nums ${timeClass}`}
                  title={formatFullLocal(t.at ?? null)}
                >
                  {timeLabel}
                </span>
              </div>

              {editingId === t.reply_id ? (
                /* Inline editor: edits Chinese, backend re-translates English,
                   status stays pending_review so the draft never leaves the queue. */
                <>
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-sub">
                      编辑草稿（中文，保存后自动重译英文）
                    </span>
                  </div>
                  <textarea
                    value={editText}
                    onChange={(e) => setEditText(e.target.value)}
                    rows={6}
                    autoFocus
                    className="w-full rounded border border-line bg-white px-3 py-2 text-[13px] leading-relaxed focus:border-accent focus:outline-none"
                  />
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      disabled={busyId != null}
                      onClick={() => saveEdit(t.reply_id)}
                      className="px-3 py-1.5 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90 disabled:opacity-50"
                    >
                      保存修改（保持待审核）
                    </button>
                    <button
                      disabled={busyId != null}
                      onClick={() => setEditingId(null)}
                      className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink disabled:opacity-50"
                    >
                      取消
                    </button>
                  </div>
                </>
              ) : (
                <>
                  {/* Full draft body with CN/EN toggle — no truncation */}
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-[11px] font-medium text-sub">
                      回复草稿（{isEn ? "英文原文" : "中文"}）
                    </span>
                    <button
                      onClick={() =>
                        setLang((prev) => ({
                          ...prev,
                          [t.reply_id]: isEn ? "cn" : "en",
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

                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      disabled={busyId === t.reply_id}
                      onClick={() => {
                        setEditingId(t.reply_id);
                        setEditText(t.content_cn ?? t.content_en ?? "");
                      }}
                      className="px-3 py-1.5 border border-accent/30 text-accent rounded text-[12px] font-medium hover:bg-accent-tint disabled:opacity-50"
                    >
                      编辑修改
                    </button>
                    <button
                      disabled={busyId === t.reply_id}
                      onClick={() => act(t.reply_id, true)}
                      className="px-3 py-1.5 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90 disabled:opacity-50"
                    >
                      审核通过并发送
                    </button>
                    <button
                      disabled={busyId === t.reply_id}
                      onClick={() => confirmReject(t.reply_id)}
                      className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink disabled:opacity-50"
                    >
                      移出待审核
                    </button>
                  </div>
                </>
              )}
            </li>
          );
        })}
      </ul>
      {error && <p className="mt-2 text-[12px] text-risk-high">{error}</p>}
    </div>
  );
}
