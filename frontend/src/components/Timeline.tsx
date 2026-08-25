import { useEffect, useRef, useState, type ReactNode } from "react";
import { dataOf, errorText, http } from "../api/client";
import { formatFullLocal, formatLocal } from "../utils/format";

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
  send_error?: string | null;
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
// combining grapheme joiner, zero-width spaces), base64/CSS debris (very long
// tokens with no spaces) and template noise (image placeholders, rule dashes,
// asterisk emphasis) make full-text look broken. Sanitize at the chunking
// entry so the English original, Chinese translation, fresh body and quoted
// history all get cleaned; real paragraphs and URLs are preserved.
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
      // Mail-template noise: plain-text parts drop image placeholders in for
      // inline logos ("[image: Lbora]" / "[图片：…]") and asterisk emphasis
      // marks ("*bold*"). A whole-line placeholder or rule (—, =, _, *) is
      // dropped; inline placeholders are removed, emphasis stars are unwrapped.
      // A bare "图片" / "image" line is a translated placeholder with no
      // content and goes the same way.
      if (/^\[(?:image|图片)[：:][ \t]*[^\]]*\]$/.test(t)) return "";
      if (/^(?:图片|image)[：:]*$/i.test(t)) return "";
      if (/^[*\-=_]{3,}$/.test(t)) return "";
      s = s
        .replace(/\[(?:image|图片)[：:][ \t]*[^\]]*\]/g, "")
        .replace(/\*([^*\r\n]+)\*/g, "$1");
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
  // Mail clients and the translator sometimes wrap a round header across two
  // lines. Two shapes occur: "…, <sender>" + a bare "wrote:" line, and the
  // long "<addr>" pair itself ("…support@shoplbora.com <" + "support@…> 写道：").
  // Rejoin each so parseRoundHead sees a single header (address + verb).
  const joined: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const cur = lines[i];
    const next = lines[i + 1];
    const curTrim = cur.trim();
    // The continuation line may itself carry a ">" quote prefix ("… <" on one
    // line, "> support@…> 写道：" on the next) — strip it so the re-joined
    // header reads as one line and parseRoundHead sees "<addr>" intact.
    const nextClean = next ? next.trim().replace(/^[>\s]+/, "") : "";
    if (
      next &&
      /^wrote[:：]|写道[:：]$/.test(nextClean) &&
      /<[^>]*@[^>]*>/.test(curTrim)
    ) {
      joined.push(`${curTrim} ${nextClean}`);
      i++;
    } else if (
      next &&
      curTrim.endsWith("<") &&
      /写道[:：]|wrote[:：]/.test(nextClean) &&
      /@/.test(nextClean)
    ) {
      joined.push(`${curTrim}${nextClean}`);
      i++;
    } else {
      joined.push(cur);
    }
  }
  const chunks: BodyChunk[] = [];
  let inQuoted = false;

  for (const raw of joined) {
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
// 超长链接（Shopify 追踪链接带 ~1.6KB 签名 token、Google Maps 也啰嗦）会撑爆
// 版面：超过显示阈值一律折叠成短标签，完整地址放 title 悬停可见、点击跳转不变。
const MAX_URL_DISPLAY = 90;

function shortUrl(url: string): string {
  if (url.length <= MAX_URL_DISPLAY) return url;
  // Shopify order-status tracking links ("…/_t/c/v3/<token>") are an opaque
  // signed wall — collapse them to a concise 查看物流 entry point instead of
  // a truncated address.
  if (/^https?:\/\/[^/]+\/_t\/c\//i.test(url)) return "查看物流";
  const domain = url.replace(/^https?:\/\//i, "").split("/")[0];
  return `${domain}/…`;
}

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
        title={url}
        className="break-all text-accent underline decoration-accent/40 hover:text-accent/80"
      >
        {shortUrl(url)}
      </a>,
    );
    last = re.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return <>{nodes}</>;
}

// The full view draws the authoritative history from the DB timeline (the
//「历史对话」fold) instead of rebuilding it from the freshest email's quoted
// copy, so a quoted reply never appears twice. Each email still keeps its own
// quoted history reachable behind the「显示引文」fold in EmailBodyView — for
// single-email conversations that quote is the only record of the earlier
// thread, so hiding it would lose the history entirely.

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

// "On Monday, August 24, 2026, 5:56 AM, support@shoplbora.com <…> wrote:" or
// the Chinese "2026年8月3日（周一）上午9:52，Lbora <…> 写道：" — the round header
// mail clients embed verbatim before each quoted round. The sender email
// inside the angle brackets is what decides the round's side.
function parseRoundHead(raw: string): { email: string; name: string; time: string } | null {
  // Quoted lines may keep a stray leading space (or a ">" for classic quotes);
  // round headers still read as "… <sender> wrote:" once trimmed. A header is
  // recognized purely by its shape — an angle-bracketed address plus the
  // wrote/写道 verb — so localised clients (中文邮箱的「写道：」) split too,
  // instead of being gated on the English "On …" opener.
  const line = raw.trim().replace(/^[>\s]+/, "");
  if (!/wrote[:：]|写道[:：]/.test(line)) return null;
  // The sender address is normally angle-bracketed ("… <support@…> wrote:"),
  // but the Chinese translation occasionally collapses the header to a bare
  // address ("…，support@… 写道："). Match the brackets first, then fall back
  // to a bare-address scan so those rounds are still attributed to a side.
  let email = "";
  let emailStart = -1;
  const angle = line.match(/<([^>]+@[^>]+)>/);
  if (angle) {
    email = angle[1].trim().toLowerCase();
    emailStart = angle.index ?? -1;
  } else {
    const bare = line.match(
      /[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/,
    );
    if (bare) {
      email = bare[0].toLowerCase();
      emailStart = bare.index ?? -1;
    }
  }
  if (!email || !email.includes("@")) return null;
  const before = line.slice(0, emailStart).trim();
  // Name is the text after the last comma: half-width in English
  // ("…, 5:56 AM, support@… wrote:") vs full-width in Chinese
  // ("…上午9:52，Lbora <…> 写道：").
  const comma = Math.max(before.lastIndexOf(","), before.lastIndexOf("，"));
  const nameRaw = comma >= 0 ? before.slice(comma + 1).trim() : "";
  // Some clients end the time part without a trailing comma ("…at 9:52 AM
  // Lbora <…>"), which leaks the timestamp into the name — treat the whole
  // "before" as the time then, and fall back to the address as the name.
  const leak = /(\d{1,2}:\d{2}[ \t]*(?:am|pm)?)|\b\d{4}\b/i.test(nameRaw)
    ? true
    : false;
  const name = leak || !nameRaw ? email : nameRaw;
  const time = formatRoundTime(leak || !nameRaw ? before : before.slice(0, comma));
  return { email, name, time };
}

// "On Monday, August 24, 2026, 5:56 AM" / "2026年8月24日（周一）凌晨12:22" — the
// date-time part of a round header, stripped of the leading "On " so it reads
// as a timestamp on the round's header row. A bare-address header leaves a
// trailing separator ("…上午7:10，" before the address), which is dropped too.
function formatRoundTime(raw: string): string {
  return raw.replace(/^on\s+/i, "").replace(/[,，]\s*$/, "").trim();
}

// The three parties that can appear inside a quoted round: the customer, our
// own support address, and any third party (Shopify order confirmations,
// automations, …). Without a support_from the caller just falls back to the
// old two-way split (customer vs our side).
type Side = "customer" | "support" | "system";

function sideOf(email: string, customer: string, support: string): Side {
  if (email === customer) return "customer";
  if (!support || email === support) return "support";
  return "system";
}

const SIDE_BADGE: Record<Side, string> = {
  customer: "bg-accent-tint text-accent",
  support: "bg-[#EFF1F4] text-sub",
  system: "bg-amber-100 text-amber-700",
};

const SIDE_LABEL: Record<Side, string> = {
  customer: "客户",
  support: "我方",
  system: "系统",
};

// One quoted line inside a round. The mail-client ">" markers are dropped —
// nesting depth is expressed by an increasing left indent instead (the round
// container already supplies the rail and its base padding).
function QuoteLine({ line }: { line: { text: string; depth: number } }) {
  return (
    <div
      className="break-words whitespace-pre-wrap"
      style={{ paddingLeft: (line.depth - 1) * 16 }}
    >
      {line.text}
    </div>
  );
}

// One-line digest of a system round's first meaningful line, so a collapsed
// Shopify template reads as a short summary instead of a wall of boilerplate.
function systemDigest(round: { body: { text: string; depth: number }[] }): string {
  for (const l of round.body) {
    const t = l.text.trim().replace(/^[>\s]+/, "");
    if (t) return t.length > 64 ? `${t.slice(0, 64)}…` : t;
  }
  return "（模板内容）";
}

// Quoted history inside a single email: a collapsible block (collapsed by
// default) that attributes each quoted round to a side. Round headers
// ("On …, <sender> wrote:" / "…写道：") split the pile into rounds; the sender
// address decides 客户 (the customer), 我方 (our support address) or 系统 (any
// other third party, e.g. Shopify automations). Quotes without such headers
// fall back to a plain wall.
function QuoteBlock({
  lines,
  customerEmail,
  supportFrom,
}: {
  lines: QuoteLine[];
  customerEmail?: string;
  supportFrom?: string;
}) {
  const [open, setOpen] = useState(false);
  // Third-party (系统) rounds — Shopify order confirmations and the like — stay
  // collapsed by default so their boilerplate doesn't bury the real question-
  // and-answer; each shows a one-line digest until clicked open.
  const [openSystems, setOpenSystems] = useState<Set<number>>(new Set());
  if (!lines.length) return null;

  const customer = (customerEmail ?? "").toLowerCase();
  const support = (supportFrom ?? "").toLowerCase();
  const rounds: {
    side: Side;
    sender: string;
    name: string;
    time: string;
    body: { text: string; depth: number }[];
  }[] = [];
  let current: (typeof rounds)[number] | null = null;
  const prelude: string[] = [];
  for (const l of lines) {
    const head = parseRoundHead(l.text);
    if (head) {
      current = {
        side: sideOf(head.email, customer, support),
        sender: head.email,
        name: head.name,
        time: head.time,
        body: [],
      };
      rounds.push(current);
    } else if (current) {
      current.body.push({ text: l.text, depth: l.depth });
    } else {
      prelude.push(l.text);
    }
  }

  const foldButton = (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      aria-expanded={open}
      className="flex items-center gap-1 text-[12px] text-sub hover:text-ink"
    >
      <span className="font-medium text-accent">{open ? "▾" : "▸"}</span>
      <span>{open ? "收起引文" : `显示引文（${lines.length} 行）`}</span>
    </button>
  );

  // No round header found (older clients quote with a bare ">" wall) — keep
  // the plain look instead of an empty attribution.
  if (!rounds.length) {
    return (
      <div className="mt-2 rounded-lg border border-line bg-[#FAFBFC] px-3 py-2">
        {foldButton}
        {open && (
          <div className="mt-2 space-y-0.5 border-l-2 border-ink/10 pl-3 text-[14px] leading-relaxed text-ink/90">
            {lines.map((l, i) => (
              <QuoteLine key={i} line={l} />
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-lg border border-line bg-[#FAFBFC] px-3 py-2">
      {foldButton}
      {open && (
        <div className="mt-2 space-y-3">
          {prelude.length > 0 && (
            <p className="break-words whitespace-pre-wrap text-[14px] leading-relaxed text-ink/90">
              {prelude.join("\n")}
            </p>
          )}
          {rounds.map((r, i) => (
            <div key={i} className="space-y-1">
              <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
                <span
                  className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${SIDE_BADGE[r.side]}`}
                >
                  {SIDE_LABEL[r.side]}
                </span>
                <span className="break-all text-[13px] font-medium text-ink">
                  {r.side === "system" ? r.sender : r.name}
                </span>
                {r.time && (
                  <span className="ml-auto shrink-0 text-[11px] text-ink/45 tabular-nums">
                    {r.time}
                  </span>
                )}
              </div>
              {r.side === "system" ? (
                openSystems.has(i) ? (
                  <>
                    <div className="space-y-0.5 border-l-2 border-ink/10 pl-3 text-[14px] leading-relaxed text-ink/90">
                      {r.body.map((line, j) => (
                        <QuoteLine key={j} line={line} />
                      ))}
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        setOpenSystems((prev) => {
                          const s = new Set(prev);
                          s.delete(i);
                          return s;
                        })
                      }
                      className="mt-0.5 text-[12px] text-ink/60 hover:text-ink"
                    >
                      ▾ 收起系统模板
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() =>
                      setOpenSystems((prev) => {
                        const s = new Set(prev);
                        s.add(i);
                        return s;
                      })
                    }
                    className="flex w-full items-start gap-1.5 rounded border border-dashed border-line bg-white/70 px-2 py-1.5 text-left transition-colors hover:bg-white"
                  >
                    <span className="mt-px shrink-0 font-medium text-amber-600">▸</span>
                    <span className="break-words text-[13px] leading-relaxed text-ink/85">
                      {systemDigest(r)}
                    </span>
                  </button>
                )
              ) : (
                <div className="space-y-0.5 border-l-2 border-ink/10 pl-3 text-[14px] leading-relaxed text-ink/90">
                  {r.body.map((line, j) => (
                    <QuoteLine key={j} line={line} />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// The email's fresh body rendered as a letter (greeting, paragraphs, lists and
// the client signature). Quoted history is folded behind a「显示引文」toggle in
// QuoteBlock instead of being dropped, so the fresh content stays readable and
// the quote is one click away — for single-email conversations it may be the
// only record of the earlier thread.
function EmailBodyView({
  text,
  customerEmail,
  supportFrom,
}: {
  text: string;
  customerEmail?: string;
  supportFrom?: string;
}) {
  const chunks = chunkEmailText(normalizeSpacing(text));
  const quotedLines = chunks
    .filter((c): c is Extract<BodyChunk, { kind: "quote" }> => c.kind === "quote")
    .flatMap((c) => c.lines);

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
      {quotedLines.length > 0 && (
        <QuoteBlock
          lines={quotedLines}
          customerEmail={customerEmail}
          supportFrom={supportFrom}
        />
      )}
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
  // Grid thumbnails hit the server-side downscale (?thumb=1) so a 3-4MB phone
  // photo downloads as a ~256px JPEG; the lightbox / download keep the original.
  const src = `/api/v1/attachments/${id}`;
  const thumbSrc = `/api/v1/attachments/${id}?thumb=1`;
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
          src={thumbSrc}
          alt={item.filename ?? "附件图片"}
          loading="lazy"
          decoding="async"
          width={64}
          height={64}
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
  fullAt,
  direction,
}: {
  label: string;
  tone: Tone;
  email?: string;
  at: string;
  fullAt?: string;
  direction?: "in" | "out";
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
      <span
        className="ml-auto shrink-0 text-[13px] text-ink tabular-nums"
        title={fullAt}
      >
        {direction === "in" ? "↓ " : direction === "out" ? "↑ " : ""}
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
      <MessageHeader
        label="我方回复"
        tone="system"
        at={formatLocal(item.at ?? null)}
        fullAt={formatFullLocal(item.at ?? null)}
        direction="out"
      />
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
  supportFrom,
}: {
  items: TimelineItem[];
  showCn: boolean;
  mode: "summary" | "full";
  customerEmail?: string;
  supportFrom?: string;
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
              fullAt={formatFullLocal(latest.at ?? null)}
              direction="in"
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
                fullAt={formatFullLocal(latestSent.at ?? null)}
                direction="out"
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
          fullAt={formatFullLocal(item.at ?? null)}
          direction="in"
        />
        {showCn ? (
          // Older emails fall back to their cached translation; only the
          // latest one triggers the on-demand translate above.
          <EmailBodyView
            text={fullCn[item.email_id!] ?? (item.content_cn || item.content || "")}
            customerEmail={customerEmail}
            supportFrom={supportFrom}
          />
        ) : (
          <EmailBodyView
            text={item.content ?? ""}
            customerEmail={customerEmail}
            supportFrom={supportFrom}
          />
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
