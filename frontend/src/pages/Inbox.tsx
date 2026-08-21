import { useCallback, useEffect, useRef, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { PendingReviewCard } from "../components/PendingReviewCard";
import { ReplyDraftEditor } from "../components/ReplyDraftEditor";
import { ReplyEditor } from "../components/ReplyEditor";
import { RiskTag } from "../components/RiskTag";
import { Timeline, TimelineItem } from "../components/Timeline";
import { formatLocal } from "../utils/format";
import { loadShowCn, saveShowCn } from "../utils/langPref";

interface InboxItem {
  id: number; // conversation id
  subject: string;
  from_email: string;
  customer_name: string | null;
  email_count: number;
  unread_count: number;
  risk_level: string | null;
  summary_cn: string | null;
  latest_status: string | null;
  latest_at: string | null;
  sla_deadline: string | null;
  sla_breached: boolean;
  sla_near: boolean;
  is_read: boolean;
}

interface ConversationData {
  id: number;
  subject: string;
  customer: { email: string; display_name: string | null };
  status: string;
  risk_level: string | null;
  retention_attempts: number;
  sla_deadline: string | null;
  suggested_merge_conversation_id: number | null;
  timeline: TimelineItem[];
}

const STATUS_LABEL: Record<string, string> = {
  pending_review: "待审核",
  draft: "草稿",
  sent: "已发送",
  failed: "发送失败",
  superseded: "已自动放行",
};

const CONV_STATUS_LABEL: Record<string, string> = {
  open: "进行中",
  escalated: "人工介入",
  resolved: "已解决",
};

const RISK_RAIL: Record<string, string> = {
  high: "bg-risk-high",
  medium: "bg-risk-medium",
  low: "bg-risk-low",
};

function Empty({ text }: { text: string }) {
  return (
    <div className="px-6 py-16 text-center text-[13px] text-sub">{text}</div>
  );
}

export default function Inbox() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [total, setTotal] = useState(0);
  const [filter, setFilter] = useState<
    "all" | "unread" | "pending_review" | "high"
  >("all");
  const [pendingCount, setPendingCount] = useState(0);
  const [keywordInput, setKeywordInput] = useState("");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const requestSeq = useRef(0);
  const itemsRef = useRef<InboxItem[]>([]);
  const pageRef = useRef(1);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [conv, setConv] = useState<ConversationData | null>(null);
  const [convLoading, setConvLoading] = useState(false);
  const [convError, setConvError] = useState("");
  const [showCn, setShowCn] = useState(loadShowCn);
  // Conversation-level 概括/全文 toggle: summary shows only the latest
  // customer email's digest; full shows its complete letter on a gray board.
  const [convMode, setConvMode] = useState<"summary" | "full">("summary");

  const load = useCallback(
    async (mode: "replace" | "append" = "replace") => {
      const seq = ++requestSeq.current;
      loadingRef.current = true;
      setLoading(true);
      try {
        // replace: re-fetch the whole visible set so fresh rows land in the
        // correct sort position (polling / initial / filter reset).
        // append: fetch the next page and merge, keeping earlier rows.
        const params: Record<string, string | number | boolean> =
          mode === "append"
            ? { page: pageRef.current + 1, size: 20 }
            : { page: 1, size: Math.max(itemsRef.current.length, 20) };
        if (filter === "pending_review") params.status = "pending_review";
        if (filter === "high") params.risk_level = "high";
        if (filter === "unread") params.unread_only = true;
        if (keyword.trim()) params.keyword = keyword.trim();
        const resp = await http.get("/inbox", { params });
        const data = dataOf<{ items: InboxItem[]; total: number }>(resp);
        if (seq !== requestSeq.current) return;
        if (mode === "append") {
          setItems((prev) => {
            const merged = new Map<number, InboxItem>();
            for (const item of prev) merged.set(item.id, item);
            for (const item of data.items) merged.set(item.id, item);
            return Array.from(merged.values());
          });
          pageRef.current += 1;
        } else {
          setItems(data.items);
        }
        setTotal(data.total);
      } finally {
        if (seq === requestSeq.current) {
          loadingRef.current = false;
          setLoading(false);
        }
      }
    },
    [filter, keyword],
  );

  function resetList() {
    requestSeq.current += 1;
    setItems([]);
    pageRef.current = 1;
  }

  useEffect(() => {
    const timer = setTimeout(() => setKeyword(keywordInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [keywordInput]);

  useEffect(() => {
    requestSeq.current += 1;
    setItems([]);
    pageRef.current = 1;
  }, [keyword]);

  // Count of conversations waiting for review, shown as a badge on the
  // "待审核" tab (independent of the active filter).
  const loadPendingCount = useCallback(async () => {
    try {
      const resp = await http.get("/inbox", {
        params: { status: "pending_review", size: 1 },
      });
      setPendingCount(dataOf<{ total: number }>(resp).total);
    } catch {
      // keep the last known count
    }
  }, []);

  useEffect(() => {
    load();
    loadPendingCount();
    const timer = setInterval(() => {
      if (!loadingRef.current) load("replace");
      loadPendingCount();
    }, 30000);
    return () => clearInterval(timer);
  }, [load, loadPendingCount]);

  // Keep a live copy of the loaded rows so a polling replace can refetch the
  // full visible set (with fresh sort order) regardless of how many were loaded.
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  // Clear the reading pane only when the filtered list becomes empty. If the
  // selected row disappears from a later page, keep the open conversation.
  useEffect(() => {
    if (items.length === 0) {
      setSelectedId(null);
      setConv(null);
    }
  }, [items]);

  const loadConv = useCallback(async (id: number) => {
    setConvLoading(true);
    setConvError("");
    try {
      const resp = await http.get(`/conversations/${id}`);
      setConv(dataOf<ConversationData>(resp));
    } catch (err) {
      setConvError(errorText(err));
      setConv(null);
    } finally {
      setConvLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId != null) loadConv(selectedId);
  }, [selectedId, loadConv]);

  // Opening a different conversation always lands on 概括 first; only a
  // deliberate toggle shows 全文 (which by then is pre-translated/cached).
  useEffect(() => {
    setConvMode("summary");
  }, [selectedId]);

  const refresh = useCallback(async () => {
    if (selectedId != null) await loadConv(selectedId);
    await load();
  }, [selectedId, loadConv, load]);

  async function select(id: number) {
    setSelectedId(id);
    const item = items.find((i) => i.id === id);
    if (item && !item.is_read) {
      try {
        await http.post(`/inbox/conversations/${id}/read`);
        window.dispatchEvent(new Event("inbox:unread-changed"));
        setItems((prev) =>
          prev.map((i) =>
            i.id === id ? { ...i, is_read: true, unread_count: 0 } : i,
          ),
        );
      } catch {
        // non-fatal: still open the conversation
      }
    }
  }

  const selectedItem = items.find((i) => i.id === selectedId) ?? null;
  const selectedIndex =
    selectedId == null ? -1 : items.findIndex((i) => i.id === selectedId);
  const prevItem = selectedIndex > 0 ? items[selectedIndex - 1] : null;
  const nextItem =
    selectedIndex >= 0 && selectedIndex < items.length - 1
      ? items[selectedIndex + 1]
      : null;

  const tabs = [
    { key: "all" as const, label: "全部" },
    { key: "unread" as const, label: "未读" },
    { key: "pending_review" as const, label: "待审核" },
    { key: "high" as const, label: "高风险" },
  ];

  return (
    <Layout>
      <div className="flex flex-wrap items-center justify-between gap-4 mb-4">
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">
          收件箱
          {total > 0 && (
            <span className="ml-2 font-normal text-sub tabular-nums">{total}</span>
          )}
        </h1>
        <div className="flex flex-wrap items-center justify-end gap-3">
          <div className="flex items-center bg-white border border-line rounded-md p-0.5">
            {tabs.map((t) => (
              <button
                key={t.key}
                onClick={() => {
                  if (filter === t.key) return;
                  setFilter(t.key);
                  resetList();
                }}
                className={`px-3 py-1 rounded text-[13px] leading-none ${
                  filter === t.key
                    ? "bg-accent text-white font-medium"
                    : "text-sub hover:text-ink"
                }`}
              >
                {t.label}
                {t.key === "pending_review" && pendingCount > 0 && (
                  <span
                    className={`ml-1 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-1 text-[11px] font-semibold tabular-nums ${
                      filter === t.key
                        ? "bg-white text-accent"
                        : "bg-risk-high text-white"
                    }`}
                  >
                    {pendingCount}
                  </span>
                )}
              </button>
            ))}
          </div>
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="搜索客户 / 主题 / 摘要"
            className="w-56 h-[30px] border border-line rounded-md bg-white px-3 text-[13px] placeholder:text-[#9AA1AB] focus:border-accent focus:outline-none"
          />
        </div>
      </div>

      <div className="flex items-start gap-4 h-[calc(100vh-164px)]">
        {/* Left: conversation list */}
        <aside className="w-[340px] shrink-0 h-full bg-white border border-line rounded-lg flex flex-col overflow-hidden">
          {items.length === 0 && loading ? (
            <Empty text="加载中…" />
          ) : items.length === 0 ? (
            <Empty text="没有匹配的会话 — 客户来信会自动归并到这里。" />
          ) : (
            <ul className="flex-1 overflow-y-auto divide-y divide-line">
              {items.map((item) => (
                <li key={item.id}>
                  <button
                    onClick={() => select(item.id)}
                    className={`relative w-full text-left px-4 py-3 transition-colors ${
                      selectedId === item.id
                        ? "bg-accent-tint"
                        : item.unread_count > 0
                          ? "bg-[#F7FAFF]"
                          : "hover:bg-[#F7F9FB]"
                    } ${item.unread_count > 0 ? "font-semibold" : "font-normal"}`}
                  >
                    {RISK_RAIL[item.risk_level ?? ""] && (
                      <span
                        className={`absolute left-0 top-0 bottom-0 w-[3px] ${
                          RISK_RAIL[item.risk_level!]
                        }`}
                      />
                    )}
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-[7px] h-[7px] shrink-0 ${
                          item.unread_count > 0 ? "bg-accent" : "bg-transparent"
                        }`}
                      />
                      <span className="text-[13px] font-semibold text-ink truncate">
                        {item.customer_name || item.from_email}
                      </span>
                      {item.unread_count > 0 && (
                        <span className="text-[11px] text-accent font-medium tabular-nums">
                          未读 {item.unread_count}
                        </span>
                      )}
                      {item.latest_status && (
                        <span className="ml-auto shrink-0 text-[11px] text-sub">
                          {STATUS_LABEL[item.latest_status] || item.latest_status}
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-[12.5px] text-sub truncate">
                      {item.subject}
                    </div>
                    <div className="mt-0.5 text-[12.5px] text-[#8A919C] truncate">
                      {item.summary_cn || "—"}
                    </div>
                    <div className="mt-1.5 flex items-center gap-2">
                      <RiskTag risk={item.risk_level} />
                      <span className="text-[11px] text-sub tabular-nums">
                        {item.email_count} 封
                      </span>
                      {item.sla_breached && (
                        <span className="px-1.5 py-0.5 rounded bg-risk-high-tint text-risk-high text-[11px] font-medium">
                          SLA 超时
                        </span>
                      )}
                      {item.sla_near && (
                        <span className="px-1.5 py-0.5 rounded bg-risk-medium-tint text-risk-medium text-[11px] font-medium">
                          SLA 临期
                        </span>
                      )}
                      <span className="ml-auto text-[11px] text-sub tabular-nums">
                        {formatLocal(item.latest_at)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="shrink-0 flex items-center justify-between px-4 py-2.5 border-t border-line text-[12px] text-sub">
            <span className="tabular-nums">共 {total} 个会话</span>
            {loading ? (
              <span>加载中…</span>
            ) : items.length < total ? (
              <button
                onClick={() => load("append")}
                className="px-2 py-0.5 border border-line rounded hover:bg-[#F7F9FB]"
              >
                加载更多
              </button>
            ) : (
              <span>已全部加载</span>
            )}
          </div>
        </aside>

        {/* Right: reading pane */}
        <section className="flex-1 min-w-0 h-full bg-white border border-line rounded-lg flex flex-col overflow-hidden">
          {selectedId == null ? (
            <Empty text="从左侧选择一个会话查看往来记录。" />
          ) : convLoading ? (
            <Empty text="加载中…" />
          ) : convError ? (
            <div className="px-6 py-10 text-center text-[13px] text-risk-high">
              {convError}
            </div>
          ) : conv ? (
            <>
              <div className="shrink-0 border-b border-line px-6 py-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <h2 className="text-[15px] font-semibold text-ink truncate">
                      {selectedItem?.subject || conv.subject}
                    </h2>
                    <p className="mt-1 text-[13px] text-sub truncate">
                      {conv.customer.display_name || "客户"} · {conv.customer.email}
                    </p>
                  </div>
                  <div className="shrink-0 flex items-center gap-2">
                    <button
                      disabled={!prevItem}
                      onClick={() => prevItem && select(prevItem.id)}
                      className="px-2.5 py-1 border border-line rounded text-[12px] text-sub hover:text-ink disabled:opacity-40 disabled:hover:text-sub"
                    >
                      ‹ 上一条
                    </button>
                    <button
                      disabled={!nextItem}
                      onClick={() => nextItem && select(nextItem.id)}
                      className="px-2.5 py-1 border border-line rounded text-[12px] text-sub hover:text-ink disabled:opacity-40 disabled:hover:text-sub"
                    >
                      下一条 ›
                    </button>
                    <RiskTag risk={conv.risk_level} />
                    <span className="px-2 py-0.5 rounded bg-[#EFF1F3] text-sub text-[11px]">
                      {CONV_STATUS_LABEL[conv.status] || conv.status}
                    </span>
                    {selectedItem && selectedItem.unread_count > 0 && (
                      <span className="text-[11px] text-accent tabular-nums">
                        {selectedItem.unread_count} 封未读
                      </span>
                    )}
                    <div className="flex items-center bg-white border border-line rounded-md p-0.5">
                      {(["summary", "full"] as const).map((m) => (
                        <button
                          key={m}
                          onClick={() => setConvMode(m)}
                          className={`px-2.5 py-1 rounded text-[12px] leading-none transition-colors ${
                            convMode === m
                              ? "bg-accent text-white font-medium"
                              : "text-sub hover:text-ink"
                          }`}
                        >
                          {m === "summary" ? "概括" : "全文"}
                        </button>
                      ))}
                    </div>
                    <button
                      onClick={() =>
                        setShowCn((v) => {
                          saveShowCn(!v);
                          return !v;
                        })
                      }
                      className="px-2.5 py-1 border border-line rounded text-[12px] text-sub hover:text-ink"
                    >
                      {showCn ? "显示英文" : "显示中文"}
                    </button>
                  </div>
                </div>
              </div>
              <div className="flex-1 px-6 py-5 overflow-y-auto">
                <PendingReviewCard items={conv.timeline} onRefresh={refresh} />
                <Timeline
                  items={conv.timeline}
                  showCn={showCn}
                  mode={convMode}
                  customerEmail={conv.customer.email}
                />
                <ReplyDraftEditor items={conv.timeline} onChanged={refresh} />
                <div className="mt-4">
                  <ReplyEditor conversationId={conv.id} onSent={refresh} />
                </div>
              </div>
            </>
          ) : null}
        </section>
      </div>
    </Layout>
  );
}
