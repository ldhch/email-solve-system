import { useState } from "react";
import { errorText, http } from "../api/client";

export function ReplyEditor({
  conversationId,
  onSent,
}: {
  conversationId: number;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);

  async function send() {
    if (!text.trim()) return;
    setError("");
    setSending(true);
    try {
      await http.post(`/conversations/${conversationId}/reply`, {
        content_cn: text.trim(),
      });
      setText("");
      onSent();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <h3 className="text-sm font-medium text-gray-700 mb-2">
        人工回复（中文，系统自动翻译为英文发送）
      </h3>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        placeholder="在这里用中文输入回复内容…"
      />
      {error && <p className="text-sm text-red-600 mt-2">{error}</p>}
      <div className="mt-2 flex justify-end">
        <button
          onClick={send}
          disabled={sending || !text.trim()}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
        >
          {sending ? "发送中…" : "翻译并发送"}
        </button>
      </div>
    </div>
  );
}
