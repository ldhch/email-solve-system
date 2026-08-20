import { useEffect, useState, type ReactNode } from "react";
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

// The full-text translation is a plain wall of Chinese text, but the email it
// came from is a real letter: greeting, paragraphs, quoted history and an
// email-client signature. Break it into those semantic blocks so it reads like
// a letter instead of one dense paragraph.
type BodyChunk =
  | { kind: "para" | "quote" | "list" | "sig"; lines: string[] };

function appendLine(chunks: BodyChunk[], kind: BodyChunk["kind"], line: string) {
  const last = chunks[chunks.length - 1];
  if (last?.kind === kind) last.lines.push(line);
  else chunks.push({ kind, lines: [line] });
}

function chunkEmailText(text: string): BodyChunk[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const chunks: BodyChunk[] = [];

  for (const raw of lines) {
    const trimmed = raw.trim();
    const last = chunks[chunks.length - 1];

    if (/^>+/.test(trimmed)) {
      appendLine(chunks, "quote", trimmed);
    } else if (/^[-*•]\s+/.test(trimmed)) {
      appendLine(chunks, "list", trimmed.replace(/^[-*•]\s+/, ""));
    } else if (!trimmed) {
      // Blank line closes the current paragraph (a marker block is filtered out).
      if (last?.kind === "para") chunks.push({ kind: "para", lines: [] });
    } else if (
      /^(Sent from|从我的)/.test(trimmed) &&
      chunks.some((c) => c.kind === "para")
    ) {
      appendLine(chunks, "sig", trimmed);
    } else {
      appendLine(chunks, "para", trimmed);
    }
  }

  return chunks.filter((c) => c.lines.some((l) => l.trim()));
}

// 正文里的 http(s) 链接还原成可点击的蓝色链接（纯文本翻译保留了 URL）。
function Linkify({ text }: { text: string }) {
  const nodes: ReactNode[] = [];
  const re = /(https?:\/\/[^\s　<>"'，。；：（）【】]+)/g;
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const url = m[1].replace(/[.,;:!?。，；：！？]+$/, "");
    nodes.push(
      <a
        key={key++}
        href={url}
        target="_blank"
        rel="noreferrer"
        className="break-all text-accent underline decoration-accent/40 hover:text-accent/80"
      >
        {url}
      </a>,
    );
    last = re.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return <>{nodes}</>;
}

function EmailBodyView({ text }: { text: string }) {
  // Renders the email's fresh body as a letter. Quoted history (`>` blocks)
  // is intentionally skipped here — the conversation timeline itself is the
  // history, so the letter shows only the new part, keeping one fold in total.
  const chunks = chunkEmailText(normalizeSpacing(text));

  return (
    <div className="space-y-2.5 text-[16px] leading-[1.7] text-ink">
      {chunks
        .filter((c) => c.kind !== "quote")
        .map((chunk, i) => {
          switch (chunk.kind) {
            case "para":
              return (
                <p key={i} className="whitespace-pre-wrap">
                  <Linkify text={chunk.lines.join("\n")} />
                </p>
              );
            case "list":
              return (
                <ul key={i} className="list-disc space-y-0.5 pl-6">
                  {chunk.lines.map((line, j) => (
                    <li key={j}>
                      <Linkify text={line} />
                    </li>
                  ))}
                </ul>
              );
            case "sig":
              return (
                <p key={i} className="text-[12.5px] text-sub">
                  <Linkify text={chunk.lines.join("\n")} />
                </p>
              );
          }
        })}
    </div>
  );
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
  // Default view: only the latest customer email stays open, everything older
  // folds behind a single "历史对话" bar; click to expand the full history.
  const [folded, setFolded] = useState(true);

  // The freshest customer email opens as a full letter by default, so the boss
  // sees the newest request at a glance without an extra click. Older emails
  // stay on their compact summary.
  useEffect(() => {
    let last: TimelineItem | undefined;
    for (const it of items)
      if (it.type === "email" && it.email_id != null) last = it;
    if (last?.email_id != null) showFull(last);
  }, []);

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

  // Fold layout: keep the oldest message open at the top and the newest few
  // open at the bottom; everything in between collapses behind one "show more"
  // bar. The bottom window is sized by how much quoted history the freshest
  // customer email carries (a long quoted thread needs more visible context).
  // Only the freshest customer email stays open by default; everything older
  // folds behind one "历史对话" bar. Anything after the last email (e.g. a
  // pending-review reply) stays open too.
  let lastEmailIdx = -1;
  items.forEach((it, i) => {
    if (it.type === "email") lastEmailIdx = i;
  });
  const openStart = lastEmailIdx < 0 ? 0 : lastEmailIdx;
  const foldable = openStart > 0;
  const historyCount = foldable ? openStart : 0;

  const renderItem = (item: TimelineItem, idx: number, history = false) => {
    const tone = toneOf(item);
    return (
      <li key={idx}>
        <div className={`rounded-lg px-4 py-3 ${CARD_STYLE[tone]}`}>
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-sub">
            <span
              className={`px-2.5 py-1 rounded text-[12.5px] font-medium ${BADGE_STYLE[tone]}`}
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

          {/* Per-email 概括/全文 toggle sits under the message type label, so
              each email card carries its own summary<->full-translation switch
              and the header no longer needs a duplicated 全文 button. It only
              makes sense in Chinese mode (English mode shows the raw letter). */}
          {item.type === "email" &&
            item.email_id != null &&
            showCn &&
            !history && (
            <div className="mb-2 flex items-center gap-1.5">
              <button
                onClick={() =>
                  setFullMode((prev) => {
                    const next = new Set(prev);
                    next.delete(item.email_id!);
                    return next;
                  })
                }
                className={`px-2.5 py-0.5 rounded text-[11.5px] transition-colors ${
                  !fullMode.has(item.email_id)
                    ? "bg-accent text-white font-medium"
                    : "border border-line text-sub hover:bg-[#F7F9FB] hover:text-ink"
                }`}
              >
                概括
              </button>
              <button
                onClick={() => {
                  if (!fullMode.has(item.email_id!)) showFull(item);
                }}
                className={`px-2.5 py-0.5 rounded text-[11.5px] transition-colors ${
                  fullMode.has(item.email_id)
                    ? "bg-accent text-white font-medium"
                    : "border border-line text-sub hover:bg-[#F7F9FB] hover:text-ink"
                }`}
              >
                全文
              </button>
            </div>
          )}

          {item.type === "email" &&
            (showCn ? (
              <div>
                {fullMode.has(item.email_id!) ? (
                  translatingId === item.email_id &&
                  !fullCn[item.email_id!] &&
                  !item.content_cn ? (
                    <div className="text-[16px] leading-normal text-sub">
                      全文翻译中…
                    </div>
                  ) : (
                    <EmailBodyView
                      text={
                        fullCn[item.email_id!] ??
                        item.content_cn ??
                        item.content ??
                        ""
                      }
                    />
                  )
                ) : (
                  <div className="text-[16px] leading-normal whitespace-pre-wrap text-ink">
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
              <div className="text-[16px] leading-[1.75] whitespace-pre-wrap text-ink">
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
  };

  return (
    <ol className="space-y-3">
      {/* Newest window: the freshest email (and anything after it) stays open. */}
      {items.slice(openStart).map((item, i) =>
        renderItem(item, openStart + i, false),
      )}
      {/* Fold bar sits right under the newest letter. */}
      {foldable && (
        <li key="fold">
          <button
            onClick={() => setFolded(!folded)}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-line bg-[#F1F3F5] px-4 py-2.5 text-[12.5px] text-sub transition-colors hover:bg-[#EAEDF0] hover:text-ink"
          >
            <span className="font-medium text-accent">{folded ? "▸" : "▾"}</span>
            <span>历史对话（更早的 {historyCount} 条）</span>
            {folded && <span className="font-medium text-accent">点击展开</span>}
          </button>
        </li>
      )}
      {/* History window, only when expanded. Older cards keep a compact look
          (no 概括/全文 toggle) so the expanded thread reads like a letter. */}
      {!folded &&
        items.slice(0, openStart).map((item, i) => renderItem(item, i, true))}
    </ol>
  );
}
