import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { ReplyDraftEditor } from "../components/ReplyDraftEditor";
import { ReplyEditor } from "../components/ReplyEditor";
import { RiskTag } from "../components/RiskTag";
import { Timeline, TimelineItem } from "../components/Timeline";
import { formatLocal } from "../utils/format";

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
  open: "进行中",
  escalated: "人工介入",
  resolved: "已解决",
};

export default function ConversationDetail() {
  const { id } = useParams();
  const conversationId = Number(id);
  const [data, setData] = useState<ConversationData | null>(null);
  const [showCn, setShowCn] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [splitOpen, setSplitOpen] = useState(false);
  const [splitEmailId, setSplitEmailId] = useState<number | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeConvId, setMergeConvId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    try {
      const resp = await http.get(`/conversations/${conversationId}`);
      setData(dataOf<ConversationData>(resp));
    } catch (err) {
      setError(errorText(err));
    }
  }, [conversationId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000); // F-02: 5s polling, no websocket
    return () => clearInterval(timer);
  }, [load]);

  async function doSplit() {
    if (!splitEmailId) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const resp = await http.post(`/conversations/${conversationId}/split`, {
        at_email_id: splitEmailId,
      });
      const res = dataOf<{ new_conversation_id: number }>(resp);
      setSplitOpen(false);
      navigate(`/conversations/${res.new_conversation_id}`);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  async function doMerge(targetId?: number) {
    const other = targetId ?? mergeConvId;
    if (!other) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await http.post(`/conversations/${conversationId}/merge`, {
        other_conversation_id: other,
      });
      setMergeOpen(false);
      setMergeConvId(null);
      setNotice(`已将会话 #${other} 合并到当前会话`);
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !data) {
    return (
      <Layout>
        <p className="text-red-600">{error}</p>
      </Layout>
    );
  }
  if (!data) {
    return (
      <Layout>
        <p className="text-gray-500">加载中…</p>
      </Layout>
    );
  }

  const emailItems = (data.timeline ?? []).filter(
    (t) => t.type === "email" && t.email_id != null,
  );
  const minEmailId = emailItems.length
    ? Math.min(...emailItems.map((t) => t.email_id as number))
    : 0;
  const splitCandidates = emailItems.filter((t) => t.email_id !== minEmailId);
  const suggestedMergeId = data.suggested_merge_conversation_id;

  return (
    <Layout>
      <div className="mb-4">
        <h1 className="text-lg font-bold">{data.subject}</h1>
        <div className="flex flex-wrap items-center gap-3 mt-2 text-sm text-gray-600">
          <span>
            {data.customer.display_name || "客户"}（{data.customer.email}）
          </span>
          <RiskTag risk={data.risk_level} />
          <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-700">
            {STATUS_LABEL[data.status] || data.status}
          </span>
          {data.retention_attempts > 0 && (
            <span className="text-purple-600">
              挽留轮次：{data.retention_attempts}/2
            </span>
          )}
          {data.sla_deadline && (
            <span className="text-red-600">
              SLA 截止：{formatLocal(data.sla_deadline)}
            </span>
          )}
          <button
            onClick={() => setShowCn((v) => !v)}
            className="ml-auto px-3 py-1 border border-gray-300 rounded text-sm"
          >
            {showCn ? "显示英文" : "显示中文"}
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {notice && <p className="text-sm text-green-600 mb-3">{notice}</p>}

      {suggestedMergeId && (
        <div className="mb-3 bg-amber-50 rounded-lg border border-amber-200 p-3 text-sm flex items-center gap-2">
          <span>
            检测到疑似同一客户：会话 <b>#{suggestedMergeId}</b>
            （同名不同邮箱），可能是同一客户的多封邮件
          </span>
          <button
            onClick={() => doMerge(suggestedMergeId)}
            disabled={busy}
            className="ml-auto shrink-0 px-3 py-1 bg-amber-500 text-white rounded text-xs disabled:opacity-50"
          >
            一键合并
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <button
          onClick={() => setSplitOpen((v) => !v)}
          disabled={!splitCandidates.length}
          title={!splitCandidates.length ? "会话邮件不足，无需拆分" : undefined}
          className="px-3 py-1 border border-gray-300 rounded text-sm disabled:opacity-40"
        >
          拆分会话
        </button>
        <button
          onClick={() => setMergeOpen((v) => !v)}
          className="px-3 py-1 border border-gray-300 rounded text-sm"
        >
          合并会话
        </button>
      </div>

      {splitOpen && (
        <div className="mb-4 bg-gray-50 rounded-lg border border-gray-200 p-3">
          <label className="block text-sm text-gray-600 mb-2">
            从哪封邮件开始拆分？该邮件及其后的往来将拆成新会话。
          </label>
          <select
            value={splitEmailId ?? ""}
            onChange={(e) => setSplitEmailId(Number(e.target.value))}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm bg-white mb-3"
          >
            <option value="" disabled>
              选择邮件…
            </option>
            {splitCandidates.map((e) => (
              <option key={e.email_id} value={e.email_id}>
                #{e.email_id} ·{" "}
                {formatLocal(e.at ?? null)} ·{" "}
                {(e.summary_cn || e.content || "").slice(0, 30)}
              </option>
            ))}
          </select>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setSplitOpen(false);
                setSplitEmailId(null);
              }}
              className="px-3 py-1 border border-gray-300 rounded text-sm"
            >
              取消
            </button>
            <button
              onClick={doSplit}
              disabled={!splitEmailId || busy}
              className="px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
            >
              确认拆分
            </button>
          </div>
        </div>
      )}

      {mergeOpen && (
        <div className="mb-4 bg-gray-50 rounded-lg border border-gray-200 p-3">
          <label className="block text-sm text-gray-600 mb-2">
            把哪个会话合并进当前会话？仅限同一客户（输入对方会话 ID）。
          </label>
          <input
            type="number"
            min={1}
            value={mergeConvId ?? ""}
            onChange={(e) =>
              setMergeConvId(e.target.value ? Number(e.target.value) : null)
            }
            placeholder="例如：3"
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm mb-3"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setMergeOpen(false);
                setMergeConvId(null);
              }}
              className="px-3 py-1 border border-gray-300 rounded text-sm"
            >
              取消
            </button>
            <button
              onClick={() => doMerge()}
              disabled={!mergeConvId || busy}
              className="px-3 py-1 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
            >
              确认合并
            </button>
          </div>
        </div>
      )}

      <Timeline items={data.timeline} showCn={showCn} onRefresh={load} />

      <ReplyDraftEditor items={data.timeline} onChanged={load} />

      <div className="mt-4">
        <ReplyEditor conversationId={data.id} onSent={load} />
      </div>
    </Layout>
  );
}
