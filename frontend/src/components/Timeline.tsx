import { http } from "../api/client";

export interface TimelineItem {
  type: "email" | "reply" | "attachment";
  direction?: string;
  email_id?: number;
  reply_id?: number;
  attachment_id?: number;
  content?: string;
  content_en?: string;
  content_cn?: string | null;
  status?: string;
  reply_type?: string;
  filename?: string;
  at?: string | null;
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
  async function approve(replyId: number) {
    await http.post(`/replies/${replyId}/approve`);
    onRefresh();
  }

  async function reject(replyId: number) {
    await http.post(`/replies/${replyId}/reject`, { reason: "" });
    onRefresh();
  }

  return (
    <ol className="space-y-4">
      {items.map((item, idx) => (
        <li key={idx} className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-2 text-xs text-gray-500">
            <span className="font-medium text-gray-700">
              {item.type === "email"
                ? "客户来信"
                : item.type === "reply"
                  ? "系统/人工回复"
                  : "附件"}
            </span>
            {item.status && (
              <span
                className={`px-1.5 py-0.5 rounded ${
                  item.status === "sent"
                    ? "bg-green-100 text-green-700"
                    : item.status === "pending_review"
                      ? "bg-blue-100 text-blue-700"
                      : item.status === "failed"
                        ? "bg-red-100 text-red-700"
                        : "bg-gray-100 text-gray-600"
                }`}
              >
                {item.status === "pending_review"
                  ? "待审核"
                  : item.status === "sent"
                    ? "已发送"
                    : item.status === "failed"
                      ? "发送失败"
                      : "草稿"}
              </span>
            )}
            {item.reply_type && item.reply_type !== "general" && (
              <span className="text-purple-600">{item.reply_type}</span>
            )}
            <span>{item.at ? item.at.replace("T", " ").replace("Z", "") : ""}</span>
          </div>

          {item.type === "email" && (
            <div className="text-sm whitespace-pre-wrap">{item.content}</div>
          )}

          {item.type === "reply" && (
            <div>
              <div className="text-sm whitespace-pre-wrap">
                {showCn && item.content_cn ? item.content_cn : item.content_en}
              </div>
              {item.status === "pending_review" && item.reply_id && (
                <div className="mt-3 flex gap-2">
                  <button
                    onClick={() => approve(item.reply_id!)}
                    className="px-3 py-1 bg-green-600 text-white rounded text-xs"
                  >
                    审核通过并发送
                  </button>
                  <button
                    onClick={() => reject(item.reply_id!)}
                    className="px-3 py-1 bg-yellow-500 text-white rounded text-xs"
                  >
                    驳回为草稿
                  </button>
                </div>
              )}
            </div>
          )}

          {item.type === "attachment" && (
            <a
              href={`/api/v1/attachments/${item.attachment_id}`}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-blue-600 underline"
            >
              📎 {item.filename}
            </a>
          )}
        </li>
      ))}
    </ol>
  );
}
