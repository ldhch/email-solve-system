import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { dataOf, http } from "../api/client";
import { Layout } from "../components/Layout";
import { formatLocal } from "../utils/format";

interface ReplyTrashItem {
  id: number;
  conversation_id: number;
  subject: string | null;
  content_en: string;
  reply_type: string;
  created_at: string | null;
}

interface TicketTrashItem {
  id: number;
  conversation_id: number;
  summary_cn: string;
  status: string;
  created_at: string | null;
}

export default function Trash() {
  const [replies, setReplies] = useState<ReplyTrashItem[]>([]);
  const [tickets, setTickets] = useState<TicketTrashItem[]>([]);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError("");
    try {
      const [r, t] = await Promise.all([
        http.get("/replies/trash"),
        http.get("/tickets/trash"),
      ]);
      setReplies(dataOf<{ items: ReplyTrashItem[] }>(r).items);
      setTickets(dataOf<{ items: TicketTrashItem[] }>(t).items);
    } catch {
      setError("加载回收站失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function restoreReply(id: number) {
    setBusyId(`r-${id}`);
    setError("");
    try {
      await http.post(`/replies/${id}/restore`);
      await load();
    } catch {
      setError("恢复回复失败，可能已超过 30 天");
    } finally {
      setBusyId(null);
    }
  }

  async function restoreTicket(id: number) {
    setBusyId(`t-${id}`);
    setError("");
    try {
      await http.post(`/tickets/${id}/restore`);
      await load();
    } catch {
      setError("恢复工单失败，可能已超过 30 天");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">回收站</h1>
      <p className="text-sm text-gray-500 mb-4">
        软删除的回复和工单保留 30 天，可在此恢复。
      </p>
      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}

      <section className="bg-white rounded-lg border border-gray-200 p-4 mb-4">
        <h2 className="font-semibold mb-3">已删除回复（{replies.length}）</h2>
        {replies.length === 0 ? (
          <p className="text-sm text-gray-500">暂无</p>
        ) : (
          <ul className="space-y-2">
            {replies.map((item) => (
              <li
                key={item.id}
                className="flex items-center gap-3 border border-gray-100 rounded px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">
                    {item.subject || "无主题"}
                  </p>
                  <p className="text-xs text-gray-500 truncate">
                    {item.content_en}
                  </p>
                  <p className="text-xs text-gray-400">
                    {formatLocal(item.created_at)}
                  </p>
                </div>
                <Link
                  to={`/conversations/${item.conversation_id}`}
                  className="text-xs text-accent hover:underline"
                >
                  打开会话
                </Link>
                <button
                  onClick={() => restoreReply(item.id)}
                  disabled={busyId === `r-${item.id}`}
                  className="px-2 py-1 border border-gray-300 rounded text-xs disabled:opacity-50"
                >
                  恢复
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="bg-white rounded-lg border border-gray-200 p-4">
        <h2 className="font-semibold mb-3">已删除工单（{tickets.length}）</h2>
        {tickets.length === 0 ? (
          <p className="text-sm text-gray-500">暂无</p>
        ) : (
          <ul className="space-y-2">
            {tickets.map((item) => (
              <li
                key={item.id}
                className="flex items-center gap-3 border border-gray-100 rounded px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">
                    {item.summary_cn}
                  </p>
                  <p className="text-xs text-gray-400">
                    {formatLocal(item.created_at)}
                  </p>
                </div>
                <Link
                  to={`/conversations/${item.conversation_id}`}
                  className="text-xs text-accent hover:underline"
                >
                  打开会话
                </Link>
                <button
                  onClick={() => restoreTicket(item.id)}
                  disabled={busyId === `t-${item.id}`}
                  className="px-2 py-1 border border-gray-300 rounded text-xs disabled:opacity-50"
                >
                  恢复
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </Layout>
  );
}
