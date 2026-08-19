import { useState } from "react";
import { errorText, http } from "../api/client";
import { TimelineItem } from "./Timeline";

type DraftItem = TimelineItem & { reply_id: number };

export function ReplyDraftEditor({
  items,
  onChanged,
}: {
  items: TimelineItem[];
  onChanged: () => void;
}) {
  const [edits, setEdits] = useState<Record<number, string>>({});
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<number | null>(null);

  const drafts = items.filter(
    (t): t is DraftItem =>
      t.type === "reply" && t.status === "draft" && t.reply_id != null,
  );

  async function save(replyId: number) {
    const content = edits[replyId];
    if (!content?.trim() || busyId != null) return;
    setError("");
    setBusyId(replyId);
    try {
      await http.patch(`/replies/${replyId}`, { content_cn: content.trim() });
      setEdits((prev) => ({ ...prev, [replyId]: "" }));
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  async function send(replyId: number) {
    if (busyId != null) return;
    setError("");
    setBusyId(replyId);
    try {
      await http.post(`/replies/${replyId}/send`);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyId(null);
    }
  }

  if (!drafts.length) return null;

  return (
    <div className="mt-4 space-y-4">
      {drafts.map((t) => (
        <div
          key={t.reply_id}
          className="bg-yellow-50 rounded-lg border border-yellow-200 p-4"
        >
          <h3 className="text-sm font-medium text-yellow-800 mb-2">
            草稿（可编辑后发送）
          </h3>
          <textarea
            value={edits[t.reply_id] ?? t.content_cn ?? t.content_en}
            onChange={(e) =>
              setEdits((prev) => ({
                ...prev,
                [t.reply_id]: e.target.value,
              }))
            }
            rows={4}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm"
          />
          <div className="mt-2 flex gap-2 justify-end">
            <button
              onClick={() => save(t.reply_id)}
              disabled={busyId != null}
              className="px-3 py-1 border border-gray-300 rounded text-sm disabled:opacity-50"
            >
              保存修改（重新翻译）
            </button>
            <button
              onClick={() => send(t.reply_id)}
              disabled={busyId != null}
              className="px-3 py-1 bg-green-600 text-white rounded text-sm disabled:opacity-50"
            >
              直接发送
            </button>
          </div>
        </div>
      ))}
      {error && <p className="text-sm text-red-600">{error}</p>}
    </div>
  );
}
