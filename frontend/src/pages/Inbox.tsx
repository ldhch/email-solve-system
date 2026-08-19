import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { RiskTag } from "../components/RiskTag";
import { Timeline, TimelineItem } from "../components/Timeline";

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

function fmtShort(iso: string | null): string {
  if (!iso) return "";
  const m = iso
    .replace("T", " ")
    .replace("Z", "")
    .match(/^\d{4}-(\d{2}-\d{2}) (\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : iso;
}

function Empty({ text }: { text: string }) {
  return (
    <div className="px-6 py-16 text-center text-[13px] text-sub">{text}</div>
  );
}

export default function Inbox() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState<"all" | "pending_review" | "high">("all");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(false);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [conv, setConv] = useState<ConversationData | null>(null);
  const [convLoading, setConvLoading] = useState(false);
  const [convError, setConvError] = useState("");
  const [showCn, setShowCn] = useState(false);

  const navigate = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = { page, size: 20 };
      if (filter === "pending_review") params.status = "pending_review";
      if (filter === "high") params.risk_level = "high";
      if (keyword.trim()) params.keyword = keyword.trim();
      const resp = await http.get("/inbox", { params });
      const data = dataOf<{ items: InboxItem[]; total: number }>(resp);
      setItems(data.items);
      setTotal(data.total);
    } finally {
      setLoading(false);
    }
  }, [page, filter, keyword]);

  useEffect(() => {
    load();
  }, [load]);

  // Keep a selection: when the list changes and the current row is gone,
  // fall back to the newest row; clear the pane when the list is empty.
  useEffect(() => {
    if (loading) return;
    if (items.length === 0) {
      setSelectedId(null);
      setConv(null);
    } else if (!items.some((i) => i.id === selectedId)) {
      setSelectedId(items[0].id);
    }
  }, [items, loading, selectedId]);

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

  const tabs = [
    { key: "all" as const, label: "全部" },
    { key: "pending_review" as const, label: "待审核" },
    { key: "high" as const, label: "高风险" },
  ];

  return (
    <Layout>
      <div className="flex items-center justify-between gap-4 mb-4">
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">
          收件箱
          {total > 0 && (
            <span className="ml-2 font-normal text-sub tabular-nums">{total}</span>
          )}
        </h1>
        <div className="flex items-center gap-3">
          <div className="flex items-center bg-white border border-line rounded-md p-0.5">
            {tabs.map((t) => (
              <button
                key={t.key}
                onClick={() => {
                  setFilter(t.key);
                  setPage(1);
                }}
                className={`px-3 py-1 rounded text-[13px] leading-none ${
                  filter === t.key
                    ? "bg-accent text-white font-medium"
                    : "text-sub hover:text-ink"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <input
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value);
              setPage(1);
            }}
            placeholder="搜索客户 / 主题 / 摘要"
            className="w-56 border border-line rounded-md bg-white px-3 py-1.5 text-[13px] placeholder:text-[#9AA1AB] focus:border-accent focus:outline-none"
          />
        </div>
      </div>

      <div className="flex items-start gap-4 h-[calc(100vh-164px)]">
        {/* Left: conversation list */}
        <aside className="w-[340px] shrink-0 h-full bg-white border border-line rounded-lg flex flex-col overflow-hidden">
          {loading ? (
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
                        : "hover:bg-[#F7F9FB]"
                    }`}
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
                      <span className="ml-auto text-[11px] text-sub tabular-nums">
                        {fmtShort(item.latest_at)}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="shrink-0 flex items-center justify-between px-4 py-2.5 border-t border-line text-[12px] text-sub">
            <span className="tabular-nums">共 {total} 个会话</span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="px-2 py-0.5 border border-line rounded disabled:opacity-40 hover:bg-[#F7F9FB]"
              >
                上一页
              </button>
              <span className="self-center tabular-nums">第 {page} 页</span>
              <button
                disabled={page * 20 >= total}
                onClick={() => setPage((p) => p + 1)}
                className="px-2 py-0.5 border border-line rounded disabled:opacity-40 hover:bg-[#F7F9FB]"
              >
                下一页
              </button>
            </div>
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
                    <RiskTag risk={conv.risk_level} />
                    <span className="px-2 py-0.5 rounded bg-[#EFF1F3] text-sub text-[11px]">
                      {CONV_STATUS_LABEL[conv.status] || conv.status}
                    </span>
                    {selectedItem && selectedItem.unread_count > 0 && (
                      <span className="text-[11px] text-accent tabular-nums">
                        {selectedItem.unread_count} 封未读
                      </span>
                    )}
                    <button
                      onClick={() => setShowCn((v) => !v)}
                      className="px-2.5 py-1 border border-line rounded text-[12px] text-sub hover:text-ink"
                    >
                      {showCn ? "显示英文" : "显示中文"}
                    </button>
                    <button
                      onClick={() => navigate(`/conversations/${conv.id}`)}
                      className="px-3 py-1 bg-accent text-white rounded text-[12px] font-medium hover:bg-accent/90"
                    >
                      打开完整会话 →
                    </button>
                  </div>
                </div>
              </div>
              <div className="flex-1 px-6 py-5 overflow-y-auto">
                <Timeline items={conv.timeline} showCn={showCn} onRefresh={refresh} />
              </div>
            </>
          ) : null}
        </section>
      </div>
    </Layout>
  );
}
