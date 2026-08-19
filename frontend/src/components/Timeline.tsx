import { useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { formatLocal } from "../utils/format";

export interface TimelineItem {
  type: "email" | "reply" | "attachment";
  direction?: string;
  email_id?: number;
  reply_id?: number;
  attachment_id?: number;
  content?: string;
  content_en?: string;
  content_cn?: string | null;
  summary_cn?: string | null;
  body_html?: string | null;
  content_type?: string | null;
  size_bytes?: number | null;
  status?: string;
  reply_type?: string;
  source?: string;
  filename?: string;
  at?: string | null;
}

const STATUS_STYLE: Record<string, string> = {
  sent: "bg-risk-low-tint text-risk-low",
  pending_review: "bg-accent-tint text-accent",
  failed: "bg-risk-high-tint text-risk-high",
  draft: "bg-[#EFF1F3] text-sub",
};

const STATUS_LABEL: Record<string, string> = {
  pending_review: "待审核",
  sent: "已发送",
  failed: "发送失败",
  draft: "草稿",
};

const TYPE_LABEL: Record<string, string> = {
  email: "客户来信",
  reply: "回复",
  attachment: "附件",
};

function typeLabel(item: TimelineItem): string {
  if (item.type === "reply") {
    if (item.source === "manual") return "人工回复";
    if (item.source === "system") return "系统回复";
  }
  return TYPE_LABEL[item.type] || item.type;
}

export function Timeline({
  items,
  showCn,
  onRefresh,
}: {
  items: TimelineItem[];
  showCn: boolean;
  onRefresh: () => void;
}) {
  // Per-email display mode (概括 vs 全文) with cached full Chinese
  // translations fetched on demand from /emails/{id}/translate.
  const [fullMode, setFullMode] = useState<Set<number>>(new Set());
  const [fullCn, setFullCn] = useState<Record<number, string>>({});
  const [translatingId, setTranslatingId] = useState<number | null>(null);
  const [translateErrors, setTranslateErrors] = useState<Record<number, string>>(
    {},
  );

  async function showFull(item: TimelineItem) {
    const id = item.email_id!;
    setFullMode((prev) => new Set(prev).add(id));
    if (fullCn[id] || item.content_cn) return;
    setTranslatingId(id);
    try {
      const resp = await http.post(`/emails/${id}/translate`);
      const data = dataOf<{ content_cn: string }>(resp);
      setFullCn((prev) => ({ ...prev, [id]: data.content_cn }));
      setTranslateErrors((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
    } catch (err) {
      setTranslateErrors((prev) => ({ ...prev, [id]: errorText(err) }));
    } finally {
      setTranslatingId(null);
    }
  }

  async function approve(replyId: number) {
    await http.post(`/replies/${replyId}/approve`);
    onRefresh();
  }

  async function reject(replyId: number) {
    await http.post(`/replies/${replyId}/reject`, { reason: "" });
    onRefresh();
  }

  return (
    <ol className="divide-y divide-line">
      {items.map((item, idx) => (
        <li key={idx} className="py-4 first:pt-0 last:pb-0">
          <div className="flex items-center gap-2 mb-1.5 text-[11.5px] text-sub">
            <span className="font-medium text-ink">
              {typeLabel(item)}
            </span>
            {item.status && (
              <span
                className={`px-1.5 py-0.5 rounded text-[11px] ${
                  STATUS_STYLE[item.status] || "bg-[#EFF1F3] text-sub"
                }`}
              >
                {STATUS_LABEL[item.status] || item.status}
              </span>
            )}
            {item.reply_type && item.reply_type !== "general" && (
              <span className="text-accent">{item.reply_type}</span>
            )}
            <span className="ml-auto tabular-nums">{formatLocal(item.at ?? null)}</span>
          </div>

          {item.type === "email" &&
            (showCn ? (
              <div>
                <div className="mb-1.5 flex items-center gap-1 text-[11px]">
                  <button
                    onClick={() =>
                      setFullMode((prev) => {
                        const next = new Set(prev);
                        next.delete(item.email_id!);
                        return next;
                      })
                    }
                    className={`px-1.5 py-0.5 rounded transition-colors ${
                      fullMode.has(item.email_id!)
                        ? "text-sub hover:text-ink"
                        : "bg-accent text-white font-medium"
                    }`}
                  >
                    概括
                  </button>
                  <button
                    onClick={() => showFull(item)}
                    className={`px-1.5 py-0.5 rounded transition-colors ${
                      fullMode.has(item.email_id!)
                        ? "bg-accent text-white font-medium"
                        : "text-sub hover:text-ink"
                    }`}
                  >
                    全文
                  </button>
                </div>
                {fullMode.has(item.email_id!) ? (
                  translatingId === item.email_id &&
                  !fullCn[item.email_id!] &&
                  !item.content_cn ? (
                    <div className="text-[13.5px] leading-relaxed text-sub">
                      全文翻译中…
                    </div>
                  ) : (
                    <div className="text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink">
                      {fullCn[item.email_id!] ?? item.content_cn ?? item.content}
                    </div>
                  )
                ) : (
                  <div className="text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink">
                    {item.summary_cn || item.content}
                  </div>
                )}
                {translateErrors[item.email_id!] && (
                  <p className="mt-1 text-[12px] text-risk-high">
                    {translateErrors[item.email_id!]}
                  </p>
                )}
              </div>
            ) : (
              <div className="text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink">
                {item.content}
              </div>
            ))}

          {item.type === "reply" && (
            <div>
              <div className="text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink">
                {showCn && item.content_cn ? item.content_cn : item.content_en}
              </div>
              {item.status === "pending_review" && item.reply_id && (
                <div className="mt-3 flex gap-2">
                  <button
                    onClick={() => approve(item.reply_id!)}
                    className="px-3 py-1.5 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90"
                  >
                    审核通过并发送
                  </button>
                  <button
                    onClick={() => reject(item.reply_id!)}
                    className="px-3 py-1.5 border border-line text-sub rounded text-[12px] hover:text-ink hover:bg-[#F7F9FB]"
                  >
                    驳回为草稿
                  </button>
                </div>
              )}
            </div>
          )}

          {item.type === "attachment" && (
            <div className="flex flex-col gap-2">
              {item.content_type?.startsWith("image/") && item.attachment_id && (
                <img
                  src={`/api/v1/attachments/${item.attachment_id}`}
                  alt={item.filename}
                  className="max-h-64 rounded border border-line"
                />
              )}
              <a
                href={`/api/v1/attachments/${item.attachment_id}`}
                target="_blank"
                rel="noreferrer"
                className="text-[13px] text-accent underline"
              >
                📎 {item.filename}
                {typeof item.size_bytes === "number"
                  ? `（${(item.size_bytes / 1024).toFixed(0)} KB）`
                  : ""}
              </a>
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}
