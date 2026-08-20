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

// A message's visual tone, so customer emails and our replies are clearly
// distinguishable at a glance (white + accent bar vs blue-tinted card).
type Tone = "email" | "system" | "manual";

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
// a letter instead of one dense paragraph. Quoted history lines keep their `>`
// nesting depth so nested rounds can be identified later.
type QuoteLine = { depth: number; text: string };
type BodyChunk =
  | { kind: "para" | "list" | "sig"; lines: string[] }
  | { kind: "quote"; lines: QuoteLine[] };

function appendText(
  chunks: BodyChunk[],
  kind: "para" | "list" | "sig",
  line: string,
) {
  const last = chunks[chunks.length - 1];
  if (last?.kind === kind) last.lines.push(line);
  else chunks.push({ kind, lines: [line] });
}

function appendQuote(chunks: BodyChunk[], line: string, depth: number) {
  const last = chunks[chunks.length - 1];
  if (last?.kind === "quote") last.lines.push({ depth, text: line });
  else chunks.push({ kind: "quote", lines: [{ depth, text: line }] });
}

// Count leading ">" markers on a quoted line, skipping whitespace between
// them (mail clients nest as "> > >" as well as ">>>").
function quoteDepth(line: string): number {
  let d = 0;
  for (const ch of line) {
    if (ch === ">") d++;
    else if (ch === " " || ch === "\t") continue;
    else break;
  }
  return d;
}

function chunkEmailText(text: string): BodyChunk[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const chunks: BodyChunk[] = [];

  for (const raw of lines) {
    const trimmed = raw.trim();
    const last = chunks[chunks.length - 1];

    if (/^>+/.test(trimmed)) {
      appendQuote(chunks, trimmed.replace(/^[>\s]+/, ""), quoteDepth(trimmed));
    } else if (/^[-*•]\s+/.test(trimmed)) {
      appendText(chunks, "list", trimmed.replace(/^[-*•]\s+/, ""));
    } else if (!trimmed) {
      // Blank line closes the current paragraph (a marker block is filtered out).
      if (last?.kind === "para") chunks.push({ kind: "para", lines: [] });
    } else if (
      /^(Sent from|从我的)/.test(trimmed) &&
      chunks.some((c) => c.kind === "para")
    ) {
      appendText(chunks, "sig", trimmed);
    } else {
      appendText(chunks, "para", trimmed);
    }
  }

  return chunks.filter((c) =>
    c.kind === "quote"
      ? c.lines.some((l) => l.text.trim())
      : c.lines.some((l) => l.trim()),
  );
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

// ---- Rebuild quoted history as a real message thread ----
// Mail clients append the whole previous thread under the newest email. Each
// round starts with a header line ("…写道：" in Chinese, "On … wrote:" in
// English) that carries the sender and a timestamp, so we can split the quote
// blob into per-sender messages, parse their times and sort them oldest-first.
interface QuoteMsg {
  fromEmail: string;
  at: string;
  content: string;
  ts: number;
}

function isRoundHead(line: string): boolean {
  return /写道：|wrote:/.test(line) && /@/.test(line);
}

function parseRoundHead(line: string): { email: string; at: string } {
  const email =
    line.match(/([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})/)?.[1] ?? "";
  const zh = line.match(/(20\d{2}年\d{1,2}月\d{1,2}日[^，,]*?)(?=[，,]\s*\S+@)/);
  const en = line.match(/^On\s+(.{1,80}?),?\s*\S+@/);
  const at = zh?.[1] ?? (en ? "On " + en[1] : line.replace(/\s*[，,]\s*\S+@.*/, "").trim());
  return { email, at };
}

function parseCnTime(s: string): number | null {
  const m = s.match(/(\d{4})年(\d{1,2})月(\d{1,2})日[^\d]{0,6}(\d{1,2})[:：](\d{1,2})/);
  if (!m) return null;
  let h = +m[4];
  if (/下午|晚上|傍晚|PM/i.test(s)) h = h === 12 ? 12 : h + 12;
  if (/上午|凌晨|早上|AM/i.test(s)) h = h === 12 ? 0 : h;
  return new Date(+m[1], +m[2] - 1, +m[3], h, +m[5]).getTime();
}

function parseEnTime(s: string): number | null {
  const m = s.match(/([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+at\s+(\d{1,2}):(\d{1,2})\s*([AP]M)/i);
  if (!m) return null;
  const months: Record<string, number> = {
    jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
    jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
  };
  const mon = months[m[1].slice(0, 3).toLowerCase()];
  if (mon === undefined) return null;
  let h = +m[4];
  if (/PM/i.test(m[6])) h = h === 12 ? 12 : h + 12;
  if (/AM/i.test(m[6])) h = h === 12 ? 0 : h;
  return new Date(+m[3], mon, +m[2], h, +m[5]).getTime();
}

// Unparseable headers sort last (kept in the thread, never dropped).
function tsOf(at: string): number {
  const t = at.startsWith("On ") ? parseEnTime(at) : parseCnTime(at);
  return t ?? Number.MAX_SAFE_INTEGER;
}

function parseQuotedRounds(lines: QuoteLine[]): QuoteMsg[] {
  const rounds: QuoteMsg[] = [];
  let cur: QuoteMsg | null = null;
  for (const { text } of lines) {
    if (isRoundHead(text)) {
      if (cur) rounds.push(cur);
      const { email, at } = parseRoundHead(text);
      cur = { fromEmail: email, at, content: "", ts: tsOf(at) };
    } else if (cur) {
      cur.content += (cur.content ? "\n" : "") + text;
    }
  }
  if (cur) rounds.push(cur);
  return rounds.sort((a, b) => a.ts - b.ts);
}

// ---- Shared pieces ----

// The email's fresh body rendered as a letter (greeting, paragraphs, lists and
// the client signature). Quoted history is intentionally excluded here — the
// Timeline lifts it into per-sender message blocks instead.
function EmailBodyView({ text }: { text: string }) {
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

// Message block header: role badge + sender email + time, so the boss sees at a
// glance who wrote what and when without reading the body.
function MessageHeader({
  label,
  tone,
  email,
  at,
}: {
  label: string;
  tone: Tone;
  email?: string;
  at: string;
}) {
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-sub">
      <span
        className={`px-2.5 py-1 rounded text-[12.5px] font-medium ${BADGE_STYLE[tone]}`}
      >
        {label}
      </span>
      {email && <span className="break-all">{email}</span>}
      <span className="ml-auto shrink-0 tabular-nums">{at}</span>
    </div>
  );
}

function HistoryBlock({
  msg,
  customerEmail,
}: {
  msg: QuoteMsg;
  customerEmail?: string;
}) {
  const isCustomer = customerEmail
    ? msg.fromEmail.toLowerCase() === customerEmail.toLowerCase()
    : false;
  const tone: Tone = isCustomer ? "email" : "system";
  return (
    <li>
      <div className={`rounded-lg px-4 py-3 ${CARD_STYLE[tone]}`}>
        <MessageHeader
          label={isCustomer ? "客户来信" : "我方回复"}
          tone={tone}
          email={msg.fromEmail}
          at={msg.at}
        />
        <div className="text-[15px] leading-[1.75] whitespace-pre-wrap text-ink">
          {msg.content}
        </div>
      </div>
    </li>
  );
}

export function Timeline({
  items,
  showCn,
  mode,
  customerEmail,
}: {
  items: TimelineItem[];
  showCn: boolean;
  mode: "summary" | "full";
  customerEmail?: string;
}) {
  // The conversation centers on the latest customer email. Summary mode shows
  // only its digest; full mode shows the whole thread rebuilt from the email's
  // quoted history as oldest-first message blocks, ending with the freshest
  // email right above the reply box. Single- and multi-email conversations
  // share this layout — only the amount of history differs.
  let latest: TimelineItem | undefined;
  for (const it of items)
    if (it.type === "email" && it.email_id != null) latest = it;

  // Full Chinese translation for that one email, cached and fetched on demand.
  const [fullCn, setFullCn] = useState<Record<number, string>>({});
  const [translating, setTranslating] = useState(false);
  const [translateError, setTranslateError] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);

  // Reset the fold when the conversation changes to a different email.
  useEffect(() => {
    setHistoryOpen(false);
  }, [latest?.email_id]);

  useEffect(() => {
    if (mode !== "full" || !showCn || !latest?.email_id) return;
    const id = latest.email_id;
    if (fullCn[id] || latest.content_cn) return;
    setTranslating(true);
    http
      .post(`/emails/${id}/translate`)
      .then((resp) => {
        const data = dataOf<{ content_cn: string }>(resp);
        setFullCn((prev) => ({ ...prev, [id]: data.content_cn }));
        setTranslateError("");
      })
      .catch((err) => setTranslateError(errorText(err)))
      .finally(() => setTranslating(false));
  }, [mode, showCn, latest?.email_id]);

  if (!latest) return null;

  const id = latest.email_id!;
  const fullText = fullCn[id] ?? latest.content_cn ?? latest.content ?? "";
  const latestAt = formatLocal(latest.at ?? null);

  if (mode === "summary") {
    return (
      <ol className="space-y-3">
        <li>
          <div className={`rounded-lg px-4 py-3 ${CARD_STYLE.email}`}>
            <MessageHeader
              label="客户来信"
              tone="email"
              email={customerEmail}
              at={latestAt}
            />
            <div className="text-[16px] leading-normal whitespace-pre-wrap text-ink">
              {showCn
                ? latest.summary_cn || normalizeSpacing(latest.content)
                : normalizeSpacing(latest.content)}
            </div>
          </div>
        </li>
      </ol>
    );
  }

  // Full mode: rebuild the quoted history into a thread.
  const displayText = showCn ? fullText : latest.content ?? "";
  const chunks = chunkEmailText(normalizeSpacing(displayText));
  const quoteLines = chunks
    .filter((c): c is Extract<BodyChunk, { kind: "quote" }> => c.kind === "quote")
    .flatMap((c) => c.lines);
  const rounds = parseQuotedRounds(quoteLines);

  return (
    <ol className="space-y-3">
      {rounds.length > 0 && (
        <li key="fold">
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-line bg-[#F1F3F5] px-4 py-2.5 text-[12.5px] text-sub transition-colors hover:bg-[#EAEDF0] hover:text-ink"
          >
            <span className="font-medium text-accent">
              {historyOpen ? "▾" : "▸"}
            </span>
            <span>历史对话（{rounds.length} 条消息）</span>
            {!historyOpen && (
              <span className="font-medium text-accent">点击展开</span>
            )}
          </button>
        </li>
      )}
      {historyOpen &&
        rounds.map((r, i) => (
          <HistoryBlock key={i} msg={r} customerEmail={customerEmail} />
        ))}
      {/* Freshest customer email, always open, right above the reply box. */}
      <li key="latest">
        <div className={`rounded-lg px-4 py-3 ${CARD_STYLE.email}`}>
          <MessageHeader
            label="客户来信"
            tone="email"
            email={customerEmail}
            at={latestAt}
          />
          {showCn ? (
            translating && !fullCn[id] && !latest.content_cn ? (
              <div className="text-[16px] leading-normal text-sub">
                全文翻译中…
              </div>
            ) : (
              <EmailBodyView text={fullText} />
            )
          ) : (
            <EmailBodyView text={latest.content ?? ""} />
          )}
          {translateError && (
            <p className="mt-1 text-[12px] text-risk-high">{translateError}</p>
          )}
        </div>
      </li>
    </ol>
  );
}
