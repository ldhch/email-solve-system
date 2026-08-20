import { useState, type ReactNode } from "react";
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

// --- 历史对话：把引用的旧邮件切成一条条消息 --------------------------
// 翻译后的历史是"时间，发件人 写道：\n<正文>" 的嵌套文本，且 `>` 层级在
// 翻译里并不一致。所以按 header 切消息、按时间重排（旧→新），再用
// 我方/客户配色区分收发，正文按书信排版。

interface HistoryMsg {
  sender: string;
  email: string | null;
  timeRaw: string;
  ts: number;
  mine: boolean;
  body: string[];
}

const HEADER_RE = /^(.*?)，(.*?)\s*(?:写道|wrote)[：:]?$/;
const DATE_RE = /^\d{4}年\d{1,2}月\d{1,2}日/;
const TIME_RE =
  /(\d{4})年(\d{1,2})月(\d{1,2})日(凌晨|早上|上午|中午|下午|晚上)?(\d{1,2})[:：](\d{1,2})/;

// 翻译出的中文时间（"2026年8月17日晚上7:17"）转成可排序的时间戳。
function parseTime(s: string): number {
  const m = s.match(TIME_RE);
  if (!m) return 0;
  const [, , , , period, h, min] = m;
  const y = +m[1];
  const mo = +m[2];
  const d = +m[3];
  let hour = +h;
  if (period === "凌晨" && hour === 12) hour = 0;
  else if ((period === "中午" || period === "下午" || period === "晚上") && hour < 12)
    hour += 12;
  return Date.UTC(y, mo - 1, d, hour, +min);
}

// 把带 `>` 前缀的历史行切成"发件人 + 正文"的消息序列。
function parseHistory(lines: string[]): HistoryMsg[] {
  const msgs: HistoryMsg[] = [];
  let cur: HistoryMsg | null = null;
  for (const raw of lines) {
    const line = raw.replace(/^>+\s*/, "").trim();
    if (!line) {
      if (cur) cur.body.push("");
      continue;
    }
    const hm = line.match(HEADER_RE);
    if (hm && DATE_RE.test(hm[1])) {
      if (cur) msgs.push(cur);
      const [, timeRaw, senderRaw] = hm;
      const em = senderRaw.match(/<([^>]+)>/);
      const email = em ? em[1] : senderRaw;
      const sender = em ? senderRaw.replace(/\s*<[^>]+>/, "").trim() : senderRaw;
      cur = {
        sender: sender || email,
        email,
        timeRaw,
        ts: parseTime(timeRaw),
        mine: /shoplbora/i.test(email || "") || /shoplbora/i.test(sender),
        body: [],
      };
    } else if (cur) {
      cur.body.push(line);
    } else {
      // 首个 header 之前的零散行（异常情况）：并成一条"未知"消息，避免丢正文
      if (!cur)
        cur = { sender: "未知发件人", email: null, timeRaw: "", ts: 0, mine: false, body: [] };
      cur.body.push(line);
    }
  }
  if (cur) msgs.push(cur);
  return msgs;
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

// 单条消息的正文：按空行切段落，首行缩进两字符（书信格式），签名识别为小字。
function MessageBody({ body }: { body: string[] }) {
  const paras: string[][] = [];
  let para: string[] = [];
  for (const line of body) {
    if (!line.trim()) {
      if (para.length) {
        paras.push(para);
        para = [];
      }
    } else {
      para.push(line);
    }
  }
  if (para.length) paras.push(para);

  return (
    <div className="mt-2 space-y-1.5 text-[15px] leading-[1.7] text-ink">
      {paras.map((p, i) => {
        const first = p[0] ?? "";
        const joined = p.join("\n");
        if (/^(Sent from|从我的)/.test(first)) {
          return (
            <p key={i} className="text-[12.5px] text-sub">
              <Linkify text={joined} />
            </p>
          );
        }
        const isGreeting = /^(亲爱的|尊敬的|Hi[,，]?|Hello[,，]?)/i.test(first);
        return (
          <p
            key={i}
            className="whitespace-pre-wrap"
            style={{ textIndent: isGreeting ? 0 : "2em" }}
          >
            <Linkify text={joined} />
          </p>
        );
      })}
    </div>
  );
}

function HistoryCard({ msg, oldest }: { msg: HistoryMsg; oldest: boolean }) {
  const mine = msg.mine;
  const initial = (msg.sender[0] || "?").toUpperCase();
  const timeShort = msg.timeRaw.replace(/^\d{4}年/, "");
  return (
    <div
      className={`rounded-md border border-line ${
        mine
          ? "border-l-4 border-l-accent bg-white"
          : "border-l-4 border-l-[#D5DAE1] bg-[#FAFBFC]"
      }`}
    >
      <div
        className={`flex items-center gap-2 px-3 py-1.5 ${
          mine ? "bg-accent/5" : "bg-[#F1F3F5]"
        }`}
      >
        <span
          className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
            mine ? "bg-accent text-white" : "bg-[#D5DAE1] text-sub"
          }`}
        >
          {initial}
        </span>
        <span className="truncate text-[13px] font-medium text-ink">{msg.sender}</span>
        {timeShort && (
          <span className="shrink-0 text-[11.5px] tabular-nums text-sub">{timeShort}</span>
        )}
        <span
          className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10.5px] font-medium ${
            mine ? "bg-accent text-white" : "bg-[#E4E7EB] text-sub"
          }`}
        >
          {mine ? "我方" : "客户"}
        </span>
      </div>
      {msg.body.length ? (
        <div className="px-3 pb-2">
          <MessageBody body={msg.body} />
        </div>
      ) : (
        <p className="px-3 pb-2 text-[12px] text-sub">
          {oldest
            ? "（最早一封邮件：发件方邮件客户端未包含其正文）"
            : "（该邮件正文缺失）"}
        </p>
      )}
    </div>
  );
}

function HistorySection({ msgs }: { msgs: HistoryMsg[] }) {
  const sorted = [...msgs].sort((a, b) => a.ts - b.ts);
  return (
    <div className="space-y-2">
      {sorted.map((msg, i) => (
        <HistoryCard key={i} msg={msg} oldest={i === 0 && msg.ts > 0} />
      ))}
    </div>
  );
}

function EmailBodyView({ text }: { text: string }) {
  const chunks = chunkEmailText(normalizeSpacing(text));
  // Quoted history (older emails the customer's mail client attached) is folded
  // behind a bar, so the boss sees only the fresh part of the letter by default.
  const [quoteOpen, setQuoteOpen] = useState(false);
  const quoteLines = chunks.filter((c) => c.kind === "quote").flatMap((c) => c.lines);
  const history = parseHistory(quoteLines);

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
      {history.length > 0 &&
        (quoteOpen ? (
          <>
            <HistorySection msgs={history} />
            <button
              onClick={() => setQuoteOpen(false)}
              className="w-full rounded border border-dashed border-line bg-[#F1F3F5] px-3 py-1 text-[12px] text-sub transition-colors hover:text-accent"
            >
              <span className="text-accent">▾</span> 收起历史对话
            </button>
          </>
        ) : (
          <button
            onClick={() => setQuoteOpen(true)}
            className="w-full rounded border border-dashed border-line bg-[#F1F3F5] px-3 py-1.5 text-[12px] text-sub transition-colors hover:text-accent"
          >
            <span className="text-accent">▸</span> 历史对话（{history.length} 封消息）点击展开
          </button>
        ))}
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
  // folds behind a single "show more" bar; click to expand the full history.
  const [folded, setFolded] = useState(true);

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

  // Fold layout: anchor on the latest inbound email (the freshest customer
  // ask). Everything older collapses behind one bar; anything after the anchor
  // (e.g. a sent reply) stays visible, and the draft editor sits after.
  const total = items.length;
  const lastEmailIdx = items.map((i) => i.type).lastIndexOf("email");
  const openFrom = lastEmailIdx >= 0 ? lastEmailIdx : Math.max(0, total - 1);
  const foldable = openFrom > 0;
  const midStart = 0;
  const midEnd = openFrom;
  const midCount = foldable ? midEnd - midStart : 0;
  const midFrom = foldable
    ? formatLocal(items[midStart].at ?? null).slice(0, 5)
    : "";
  const midTo = foldable
    ? formatLocal(items[midEnd - 1].at ?? null).slice(0, 5)
    : "";
  const midRange = midFrom && midTo ? `${midFrom} – ${midTo}` : "";

  return (
    <ol className="space-y-3">
      {items.map((item, idx) => {
        // Folded older section: render one "show more" bar in place of the
        // messages before the anchor email, skip the rest until expanded.
        if (foldable && folded && idx >= midStart && idx < midEnd) {
          if (idx === midStart) {
            return (
              <li key={`fold:${idx}`}>
                <button
                  onClick={() => setFolded(false)}
                  className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-line bg-[#F1F3F5] px-4 py-2.5 text-[12.5px] text-sub transition-colors hover:bg-[#EAEDF0] hover:text-ink"
                >
                  <span className="font-medium text-accent">▸</span>
                  <span>更早的 {midCount} 条消息</span>
                  {midRange && <span>{midRange}</span>}
                  <span className="font-medium text-accent">点击展开</span>
                </button>
              </li>
            );
          }
          return null;
        }
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
      })}
    </ol>
  );
}
