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

// A message's visual tone, so inbound emails, system replies and manual
// replies are clearly distinguishable at a glance (white + accent bar vs
// blue-tinted vs green-tinted card).
type Tone = "email" | "system" | "manual";

function toneOf(item: TimelineItem): Tone {
  if (item.type === "reply") {
    if (item.source === "manual") return "manual";
    if (item.source === "system") return "system";
  }
  return "email";
}

const CARD_STYLE: Record<Tone, string> = {
  email: "bg-white border border-line border-l-4 border-l-accent",
  system: "bg-accent-tint border border-accent/15",
  manual: "bg-risk-low-tint border border-risk-low/20",
};

const BADGE_STYLE: Record<Tone, string> = {
  email: "bg-accent-tint text-accent",
  system: "bg-accent text-white",
  manual: "bg-risk-low text-white",
};

function typeLabel(item: TimelineItem): string {
  if (item.type === "reply") {
    if (item.source === "manual") return "人工回复";
    if (item.source === "system") return "系统回复";
  }
  return TYPE_LABEL[item.type] || item.type;
}

// Raw email bodies sometimes carry HTML entities, double blank lines and
// stray indentation (plain-text parts keep their original CRLF line endings).
// Normalize for display: drop &nbsp;, collapse to single newlines, keep at
// most one blank line between paragraphs, and strip leading whitespace so
// every line starts flush at the same left column.
function normalizeSpacing(text?: string | null): string {
  return (text ?? "")
    .replace(/&nbsp;/gi, " ")
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/^[ \t]+/gm, "")
    .replace(/\n{3,}/g, "\n\n");
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
    <ol className="space-y-3">
      {items.map((item, idx) => {
        const tone = toneOf(item);
        return (
          <li key={idx}>
            <div className={`rounded-lg px-4 py-3 ${CARD_STYLE[tone]}`}>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-sub">
                <span
                  className={`px-2 py-0.5 rounded font-medium ${BADGE_STYLE[tone]}`}
                >
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
                <span className="ml-auto tabular-nums">
                  {formatLocal(item.at ?? null)}
                </span>
              </div>

              {item.type === "email" &&
                (showCn ? (
                  <div>
                    <div className="mb-2 flex items-center gap-1 text-[12px]">
                      <button
                        onClick={() =>
                          setFullMode((prev) => {
                            const next = new Set(prev);
                            next.delete(item.email_id!);
                            return next;
                          })
                        }
                        className={`px-2 py-0.5 rounded transition-colors ${
                          fullMode.has(item.email_id!)
                            ? "text-sub hover:text-ink"
                            : "bg-accent text-white font-medium"
                        }`}
                      >
                        概括
                      </button>
                      <button
                        onClick={() => showFull(item)}
                        className={`px-2 py-0.5 rounded transition-colors ${
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
                        <div className="text-[15px] leading-normal text-sub">
                          全文翻译中…
                        </div>
                      ) : (
                        <div className="text-[15px] leading-normal whitespace-pre-wrap text-ink">
                          {normalizeSpacing(
                            fullCn[item.email_id!] ?? item.content_cn ?? item.content,
                          )}
                        </div>
                      )
                    ) : (
                      <div className="text-[15px] leading-normal whitespace-pre-wrap text-ink">
                        {item.summary_cn || normalizeSpacing(item.content)}
                      </div>
                    )}
                    {translateErrors[item.email_id!] && (
                      <p className="mt-1 text-[12px] text-risk-high">
                        {translateErrors[item.email_id!]}
                      </p>
                    )}
                  </div>
                ) : (
                  <div className="text-[15px] leading-[1.75] whitespace-pre-wrap text-ink">
                    {normalizeSpacing(item.content)}
                  </div>
                ))}

              {item.type === "reply" && (
                <div>
                  <div
                    className={`text-[15px] whitespace-pre-wrap text-ink ${
                      showCn && item.content_cn
                        ? "leading-normal"
                        : "leading-[1.75]"
                    }`}
                  >
                    {showCn && item.content_cn
                      ? item.content_cn
                      : item.content_en}
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
                  {item.content_type?.startsWith("image/") &&
                    item.attachment_id && (
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
                    className="text-[13.5px] text-accent underline"
                  >
                    📎 {item.filename}
                    {typeof item.size_bytes === "number"
                      ? `（${(item.size_bytes / 1024).toFixed(0)} KB）`
                      : ""}
                  </a>
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
