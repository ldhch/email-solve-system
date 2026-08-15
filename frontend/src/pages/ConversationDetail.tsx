import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";
import { ReplyEditor } from "../components/ReplyEditor";
import { RiskTag } from "../components/RiskTag";
import { Timeline, TimelineItem } from "../components/Timeline";

interface ConversationData {
  id: number;
  subject: string;
  customer: { email: string; display_name: string | null };
  status: string;
  risk_level: string | null;
  retention_attempts: number;
  sla_deadline: string | null;
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
  const [edits, setEdits] = useState<Record<number, string>>({});

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

  async function editDraft(replyId: number) {
    const content = edits[replyId];
    if (!content?.trim()) return;
    setError("");
    try {
      await http.patch(`/replies/${replyId}`, { content_cn: content.trim() });
      setEdits((prev) => ({ ...prev, [replyId]: "" }));
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function sendDraft(replyId: number) {
    setError("");
    try {
      await http.post(`/replies/${replyId}/send`);
      await load();
    } catch (err) {
      setError(errorText(err));
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
              SLA 截止：{data.sla_deadline.replace("T", " ").replace("Z", "")}
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

      <Timeline items={data.timeline} showCn={showCn} onRefresh={load} />

      {/* Rejected drafts: edit + send */}
      {data.timeline
        .filter((t) => t.type === "reply" && t.status === "draft" && t.reply_id)
        .map((t) => (
          <div
            key={t.reply_id}
            className="mt-4 bg-yellow-50 rounded-lg border border-yellow-200 p-4"
          >
            <h3 className="text-sm font-medium text-yellow-800 mb-2">
              草稿（可编辑后发送）
            </h3>
            <textarea
              defaultValue={t.content_cn || t.content_en}
              onChange={(e) =>
                setEdits((prev) => ({
                  ...prev,
                  [t.reply_id!]: e.target.value,
                }))
              }
              rows={4}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm"
            />
            <div className="mt-2 flex gap-2 justify-end">
              <button
                onClick={() => editDraft(t.reply_id!)}
                className="px-3 py-1 border border-gray-300 rounded text-sm"
              >
                保存修改（重新翻译）
              </button>
              <button
                onClick={() => sendDraft(t.reply_id!)}
                className="px-3 py-1 bg-green-600 text-white rounded text-sm"
              >
                直接发送
              </button>
            </div>
          </div>
        ))}

      <div className="mt-4">
        <ReplyEditor conversationId={data.id} onSent={load} />
      </div>
    </Layout>
  );
}
