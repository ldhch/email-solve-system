import { useEffect, useRef, useState, type ReactNode } from "react";
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
  low_confidence?: boolean;
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

// A browser/network timeout on the on-demand translate call is NOT a hard
// failure: the backend keeps translating in a worker thread and caches the
// result, so the frontend switches to polling the status endpoint instead.
// Hard errors (LLM_FAILED, NOT_FOUND, ...) still surface immediately.
function isTimeoutError(err: unknown): boolean {
  return (
    (typeof err === "object" &&
      err !== null &&
      (err as { code?: string }).code === "ECONNABORTED") ||
    (err instanceof Error && /timeout/i.test(err.message))
  );
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

// Embedded HTML tags (<b>/<i>…), invisible markers mailers inject (U+035F
// combining grapheme joiner, zero-width spaces) and base64/CSS debris (very
// long tokens with no spaces) make full-text look broken. Sanitize at the
// chunking entry so the English original, Chinese translation, fresh body and
// quoted history all get cleaned; real paragraphs and URLs are preserved.
function sanitizeText(text: string): string {
  return text
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => {
      // Strip HTML tags but keep quoted-header emails like "<user@host>",
      // which look like tags but carry the sender's address (their "@" makes
      // the quote-round head detectable). A tag that itself contains an "@"
      // (rare, e.g. mailto hrefs) is left in place rather than eating the email.
      let s = line.replace(/<(?![^>]*@)[^>]*>/g, "");
      s = s.replace(/[\u034f\u00ad\u200b\u200c\u200d\u2060\ufeff]/g, "");
      const t = s.trim();
      if (!t) return "";
      // base64 / inline-CSS debris: very long, no spaces, not a URL.
      if (t.length > 200 && !/\s/.test(t) && !/^[a-z]+:\/\//i.test(t)) return "";
      return s;
    })
    .join("\n");
}

// Detect an unmarked quoted header ("On …, <addr> wrote:" / "…写道：" with no
// ">" prefix), which Yahoo/Gmail embed inline. chunkEmailText treats it (and
// everything after it) as quoted history so embedded HTML/CSS never leaks into
// the fresh body. This is part of quoted-history *stripping* and must stay.
function isRoundHead(line: string): boolean {
  return /写道：|wrote:/.test(line) && /@/.test(line);
}

function chunkEmailText(text: string): BodyChunk[] {
  const lines = sanitizeText(text).split("\n");
  const chunks: BodyChunk[] = [];
  let inQuoted = false;

  for (const raw of lines) {
    const trimmed = raw.trim();
    const last = chunks[chunks.length - 1];

    if (/^>+/.test(trimmed)) {
      // Classic ">" quoting — once seen, everything after stays quoted.
      if (!inQuoted) inQuoted = true;
      appendQuote(chunks, trimmed.replace(/^[>\s]+/, ""), quoteDepth(trimmed));
    } else if (isRoundHead(trimmed)) {
      // Unmarked quote header ("On … wrote:" with no ">" prefix) — Yahoo and
      // Gmail embed the previous thread inline; treat it and everything after
      // as quoted history so embedded HTML/CSS never leaks into the fresh body.
      inQuoted = true;
      appendQuote(chunks, trimmed.replace(/^[>\s]+/, ""), quoteDepth(trimmed));
    } else if (inQuoted) {
      appendQuote(chunks, trimmed, 0);
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

// Quoted-history *stripping* lives in chunkEmailText / EmailBodyView (kept
// untouched): a customer reply that quotes our Hostinger mail shows only its
// fresh content. The full view no longer rebuilds message blocks from that
// quoted copy — the「历史对话」fold draws from the authoritative DB timeline
// instead, so a quoted reply never appears twice.

// Summary mode shows the Chinese digest; if none was generated, fall back to
// the freshest part of the body with quoted history stripped, so the boss
// never sees the raw ">" thread in the digest.
function freshPreview(content?: string | null): string {
  const chunks = chunkEmailText(normalizeSpacing(content));
  return chunks
    .filter(
      (c): c is Extract<BodyChunk, { kind: "para" | "list" | "sig" }> =>
        c.kind !== "quote",
    )
    .flatMap((c) => c.lines)
    .join("\n")
    .slice(0, 300);
}

// ---- Shared pieces ----

// The email's fresh body rendered as a letter (greeting, paragraphs, lists and
// the client signature). Quoted history is intentionally excluded here — the
// Timeline lifts it into per-sender message blocks instead.
function EmailBodyView({ text }: { text: string }) {
  const chunks = chunkEmailText(normalizeSpacing(text));

  return (
    <div className="space-y-2 text-[16px] leading-[1.5] text-ink">
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

// ---- Email attachments ----
// Customer emails often carry photos (damaged goods, order notes). The
// conversation timeline already exposes them as `type: "attachment"` entries;
// this renders each one: images as square thumbnails that open a lightbox on
// click, everything else (PDF/zip/…) as a file chip linking to the download
// endpoint. Thumbnails lazy-load so a thread with many images stays light.

function fmtBytes(n: number | null | undefined): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function Lightbox({
  src,
  title,
  onClose,
}: {
  src: string;
  title?: string;
  onClose: () => void;
}) {
  // Close on Esc as well as on backdrop click; the image itself stops the
  // click so zoomed content can be inspected without losing it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title ?? "附件大图"}
    >
      <div className="max-w-full" onClick={(e) => e.stopPropagation()}>
        <img
          src={src}
          alt={title ?? "附件图片"}
          className="max-h-[85vh] max-w-full rounded-lg object-contain"
        />
        {title && (
          <div className="mt-2 truncate text-center text-[12px] text-white/80">
            {title}
          </div>
        )}
      </div>
    </div>
  );
}

function AttachmentThumb({ item }: { item: TimelineItem }) {
  const [open, setOpen] = useState(false);
  const id = item.attachment_id;
  if (id == null) return null;
  const src = `/api/v1/attachments/${id}`;
  const isImage =
    (item.content_type ?? "").startsWith("image/") ||
    /\.(jpe?g|png|gif|webp|bmp|avif|svg)$/i.test(item.filename ?? "");
  if (!isImage) {
    return (
      <a
        href={src}
        target="_blank"
        rel="noreferrer"
        className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line bg-[#F7F9FB] px-2 py-1 text-[12px] text-sub transition-colors hover:text-ink"
        title={`下载 ${item.filename}`}
      >
        <span>📄</span>
        <span className="truncate">{item.filename}</span>
        {item.size_bytes != null && (
          <span className="shrink-0 tabular-nums">{fmtBytes(item.size_bytes)}</span>
        )}
      </a>
    );
  }
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group relative h-16 w-16 shrink-0 overflow-hidden rounded-lg border border-line bg-[#F1F3F5] transition-shadow hover:shadow-md"
        title={`点击放大：${item.filename}`}
      >
        <img
          src={src}
          alt={item.filename ?? "附件图片"}
          loading="lazy"
          className="h-full w-full object-cover transition-transform group-hover:scale-105"
        />
      </button>
      {open && (
        <Lightbox src={src} title={item.filename} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

function AttachmentGrid({ items }: { items: TimelineItem[] }) {
  const attachments = items.filter(
    (it): it is TimelineItem & { attachment_id: number } =>
      it.type === "attachment" && it.attachment_id != null,
  );
  if (attachments.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="mb-1.5 text-[11.5px] font-medium text-sub">附件</div>
      <div className="flex flex-wrap items-start gap-2">
        {attachments.map((a) => (
          <AttachmentThumb key={a.attachment_id} item={a} />
        ))}
      </div>
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
      {email && (
        <span className="break-all text-[13px] font-medium text-ink">
          {email}
        </span>
      )}
      <span className="ml-auto shrink-0 text-[13px] text-ink tabular-nums">
        {at}
      </span>
    </div>
  );
}

// A sent reply from our side (manual or AI): blue-tinted card with a
// 「√ 已发送」status badge and its origin (人工 / 自动), so the boss can verify
// what was actually sent without opening the mailbox. Drafts / pending-review
// replies are handled by PendingReviewCard and ReplyDraftEditor, not here.
function ReplyBlock({ item, showCn }: { item: TimelineItem; showCn: boolean }) {
  const text = showCn
    ? item.content_cn || item.content_en || ""
    : item.content_en || "";
  const isManual = item.source === "manual";
  return (
    <div className={`rounded-lg px-4 py-3 ${CARD_STYLE.system}`}>
      <MessageHeader label="我方回复" tone="system" at={formatLocal(item.at ?? null)} />
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-sub">
        <span className="inline-flex items-center rounded bg-green-100 px-1.5 py-0.5 font-medium text-green-700">
          √ 已发送
        </span>
        <span>{isManual ? "人工回复" : "自动回复"}</span>
      </div>
      {text && (
        <div className="text-[15px] leading-[1.45] whitespace-pre-wrap text-ink">
          {text}
        </div>
      )}
    </div>
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
  // One automatic retry per email when the first on-demand call fails: long
  // emails can run close to the request timeout even when the model succeeds.
  const [retryTick, setRetryTick] = useState(0);
  // When the on-demand POST exhausts its retries on a timeout, the backend
  // keeps translating off-thread; poll the status endpoint until the Chinese
  // appears so the boss never has to reopen the email manually.
  const [pollingId, setPollingId] = useState<number | null>(null);
  const translateAttemptsRef = useRef<Record<number, number>>({});

  // Reset the fold when the conversation changes to a different email.
  useEffect(() => {
    setHistoryOpen(false);
  }, [latest?.email_id]);

  // Request the full-text translation on demand. A generous per-request
  // timeout (120s) beats the global 60s default for long emails; a single
  // automatic retry covers transient empty-response failures. A remaining
  // timeout hands off to status polling; only a hard error is surfaced.
  useEffect(() => {
    if (mode !== "full" || !showCn || !latest?.email_id) return;
    const id = latest.email_id;
    if (fullCn[id] || latest.content_cn) return;
    setTranslating(true);
    const attempts = translateAttemptsRef.current[id] ?? 0;
    http
      .post(`/emails/${id}/translate`, undefined, { timeout: 120000 })
      .then((resp) => {
        const data = dataOf<{ content_cn: string }>(resp);
        setFullCn((prev) => ({ ...prev, [id]: data.content_cn }));
        setTranslateError("");
        translateAttemptsRef.current[id] = 0;
      })
      .catch((err) => {
        const next = attempts + 1;
        translateAttemptsRef.current[id] = next;
        if (next < 2) {
          setRetryTick((t) => t + 1); // one automatic retry
        } else if (isTimeoutError(err)) {
          setPollingId(id); // backend still translating in a worker thread
        } else {
          setTranslateError(errorText(err)); // hard failure, no point polling
        }
      })
      .finally(() => setTranslating(false));
  }, [mode, showCn, latest?.email_id, retryTick]);

  // Poll the translation status while the on-demand call is stuck on a
  // timeout. The backend keeps translating off-thread (and the prefill job
  // may also pick the email up), so the Chinese eventually appears without
  // the boss reopening the email. Bounded so a permanently failing
  // translation surfaces an error instead of polling forever.
  useEffect(() => {
    if (pollingId == null) return;
    let cancelled = false;
    let polls = 0;
    const timer = setInterval(async () => {
      if (cancelled) return;
      polls += 1;
      try {
        const resp = await http.get(`/emails/${pollingId}/translate/status`);
        const data = dataOf<{ status: string; content_cn: string | null }>(
          resp,
        );
        if (data.status === "done" && data.content_cn) {
          cancelled = true;
          setFullCn((prev) => ({ ...prev, [pollingId]: data.content_cn! }));
          setTranslateError("");
          setPollingId(null);
          setTranslating(false);
          return;
        }
      } catch {
        // the status endpoint is read-only; a transient error just means we
        // poll again on the next tick
      }
      if (polls >= 24) {
        cancelled = true;
        setPollingId(null);
        setTranslating(false);
        setTranslateError(
          "翻译未能在预期时间内完成，请稍后重新打开查看（后台会继续翻译并缓存）。",
        );
      }
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [pollingId]);

  if (!latest) return null;

  const id = latest.email_id!;
  const latestAt = formatLocal(latest.at ?? null);
  // Attachments may belong to any email in the thread; show them all under the
  // freshest customer email so the boss sees the photos without digging.
  const attachments = items.filter((it) => it.type === "attachment");
  // Latest sent reply, shown as a「我方已回复」digest in summary mode so the boss
  // confirms what was sent without opening the full thread.
  const sentReplies = items
    .filter(
      (it): it is TimelineItem & { reply_id: number } =>
        it.type === "reply" && it.status === "sent" && it.reply_id != null,
    )
    .sort((a, b) => (a.at ?? "").localeCompare(b.at ?? ""));
  const latestSent = sentReplies[sentReplies.length - 1] ?? null;

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
              {latest.summary_cn || freshPreview(latest.content)}
            </div>
            <AttachmentGrid items={attachments} />
          </div>
        </li>
        {latestSent && (
          <li>
            <div className={`rounded-lg px-4 py-3 ${CARD_STYLE.system}`}>
              <MessageHeader
                label="我方已回复"
                tone="system"
                at={formatLocal(latestSent.at ?? null)}
              />
              <p className="text-[15px] leading-normal whitespace-pre-wrap text-ink line-clamp-3">
                {(latestSent.content_cn ||
                  latestSent.content_en ||
                  "（已发送回复）")
                  .replace(/\s+/g, " ")
                  .trim()}
              </p>
            </div>
          </li>
        )}
      </ol>
    );
  }

  // The whole conversation as an interleaved message thread: every customer
  // email and our sent replies, ordered by time. Drafts / pending-review
  // replies stay with PendingReviewCard and ReplyDraftEditor.
  const threadItems = items
    .filter(
      (it) =>
        it.type === "email" ||
        (it.type === "reply" && it.status === "sent"),
    )
    .sort((a, b) => (a.at ?? "").localeCompare(b.at ?? ""));

  // The full view keeps only the latest question-and-answer open — the newest
  // customer email plus any replies sent after it — and folds everything older
  // into the「历史对话」collapse. The fold draws from the authoritative DB
  // timeline instead of re-parsing the freshest email's quoted copy, so when a
  // customer reply quotes our Hostinger mail the same message never appears
  // twice. (Quoted-history stripping inside the email body view is untouched.)
  const latestEmailIdx = threadItems.reduce(
    (idx, it, i) => (it.type === "email" ? i : idx),
    -1,
  );
  const foldIdx = latestEmailIdx < 0 ? 0 : latestEmailIdx;
  const historyItems = threadItems.slice(0, foldIdx);
  const visibleItems = threadItems.slice(foldIdx);

  const renderEmail = (item: TimelineItem, isLatest: boolean, keyPrefix: string) => (
    <li key={`${keyPrefix}${item.email_id}`}>
      <div className={`rounded-lg px-4 py-3 ${CARD_STYLE.email}`}>
        <MessageHeader
          label="客户来信"
          tone="email"
          email={customerEmail}
          at={formatLocal(item.at ?? null)}
        />
        {showCn ? (
          // Older emails fall back to their cached translation; only the
          // latest one triggers the on-demand translate above.
          <EmailBodyView
            text={fullCn[item.email_id!] ?? (item.content_cn || item.content || "")}
          />
        ) : (
          <EmailBodyView text={item.content ?? ""} />
        )}
        {isLatest && <AttachmentGrid items={attachments} />}
        {isLatest && translating && !fullCn[id] && !latest.content_cn && (
          <p className="mt-1.5 text-[12px] text-sub">
            中文翻译生成中，先显示英文原文…
          </p>
        )}
        {isLatest && translateError && (
          <p className="mt-1 text-[12px] text-risk-high">{translateError}</p>
        )}
      </div>
    </li>
  );

  return (
    <ol className="space-y-3">
      {historyItems.length > 0 && (
        <li key="fold">
          <button
            onClick={() => setHistoryOpen((v) => !v)}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-line bg-[#F1F3F5] px-4 py-2.5 text-[12.5px] text-sub transition-colors hover:bg-[#EAEDF0] hover:text-ink"
          >
            <span className="font-medium text-accent">
              {historyOpen ? "▾" : "▸"}
            </span>
            <span>历史对话（{historyItems.length} 条消息）</span>
            {!historyOpen && (
              <span className="font-medium text-accent">点击展开</span>
            )}
          </button>
        </li>
      )}
      {historyOpen &&
        historyItems.map((item) =>
          item.type === "email"
            ? renderEmail(item, false, "he-")
            : (
                <li key={`hr-${item.reply_id}`}>
                  <ReplyBlock item={item} showCn={showCn} />
                </li>
              ),
        )}
      {visibleItems.map((item) =>
        item.type === "email"
          ? renderEmail(item, item.email_id === latest?.email_id, "e-")
          : (
              <li key={`r-${item.reply_id}`}>
                <ReplyBlock item={item} showCn={showCn} />
              </li>
            ),
      )}
    </ol>
  );
}
